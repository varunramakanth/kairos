"""The HTTP API: threads, runs and the typed run event stream (ADR-002).

Every request and response body is a Pydantic model, so ``/openapi.json`` describes
the whole surface, including the SSE event union, and the web UI's TypeScript
client is generated from it (``just gen-client``).

Run it with ``just api`` (which also starts an embedded worker) or
``uvicorn --factory kairos_api.app:create_app``.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_api.settings import ApiSettings
from kairos_api.streams import run_event_stream
from kairos_db import (
    EventListener,
    NotFoundError,
    RunReader,
    ThreadBusyError,
    ThreadRepository,
    create_engine,
)
from kairos_domain.base import DomainModel
from kairos_domain.content import Message
from kairos_domain.events import RunEvent
from kairos_domain.run import RunRequest
from kairos_domain.threads import RunDetail, Thread, ThreadDetail
from kairos_harness import Provider
from kairos_worker.service import open_runtime, work

API_VERSION = "0.2.0"


# --- request and response bodies ------------------------------------------------------


class NewMessage(DomainModel):
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    model: str | None = None
    """Override the default model for this turn."""


class RunAccepted(DomainModel):
    thread_id: str
    run_id: str


class Blob(DomainModel):
    id: str
    content: str


class Health(DomainModel):
    status: str
    version: str


# --- application ----------------------------------------------------------------------


@dataclass
class Services:
    engine: AsyncEngine
    threads: ThreadRepository
    runs: RunReader
    listener: EventListener


def _operation_id(route: APIRoute) -> str:
    """Use the function name, so the generated client has ``listThreads`` and the like."""
    return route.name


def _title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "New chat"
    return first_line if len(first_line) <= 80 else first_line[:77] + "..."


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _frame(event: RunEvent) -> RunEvent:
    """Wrap an event in an SSE frame whose ``id`` is its ``seq``.

    Durable events get an ``id`` so a reconnecting ``EventSource`` sends it back as
    ``Last-Event-ID``. FastAPI sends ``ServerSentEvent`` objects as they are, while
    the route's ``RunEvent`` annotation documents the ``data`` payload in OpenAPI;
    the cast keeps that annotation honest about the payload type.
    """
    frame = ServerSentEvent(data=event, id=str(event.seq) if event.seq is not None else None)
    return cast("RunEvent", frame)


def create_app(
    settings: ApiSettings | None = None,
    *,
    provider_factory: Callable[[], Provider] | None = None,
) -> FastAPI:
    """Build the app. ``provider_factory`` replaces the model provider (tests, replay)."""
    settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings.database_url)
        listener = EventListener(settings.database_url)
        await listener.start()
        app.state.services = Services(
            engine=engine,
            threads=ThreadRepository(engine),
            runs=RunReader(engine),
            listener=listener,
        )
        stop = asyncio.Event()
        async with AsyncExitStack() as stack:
            stack.push_async_callback(engine.dispose)
            stack.push_async_callback(listener.close)
            if settings.embedded_worker:
                provider = provider_factory() if provider_factory else None
                runtime = await stack.enter_async_context(open_runtime(settings, provider=provider))
                task = asyncio.create_task(work(runtime, engine, stop))

                async def stop_worker() -> None:
                    stop.set()
                    await task

                stack.push_async_callback(stop_worker)
            yield

    app = FastAPI(
        title="Kairos API",
        version=API_VERSION,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ThreadBusyError)
    async def busy(request: Request, error: ThreadBusyError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    def services(request: Request) -> Services:
        return cast("Services", request.app.state.services)

    def build_request(
        thread_id: str, body: NewMessage
    ) -> Callable[[tuple[Message, ...]], RunRequest]:
        def build(history: tuple[Message, ...]) -> RunRequest:
            return RunRequest(
                run_id=_new_id("run"),
                thread_id=thread_id,
                input=body.text,
                model=body.model or settings.model,
                history=history,
            )

        return build

    @app.get("/health", tags=["meta"])
    async def health() -> Health:
        return Health(status="ok", version=API_VERSION)

    @app.get("/threads", tags=["threads"])
    async def list_threads(request: Request) -> list[Thread]:
        return await services(request).threads.recent()

    @app.post("/threads", status_code=201, tags=["threads"])
    async def create_thread(request: Request, body: NewMessage) -> RunAccepted:
        """Start a conversation with its first message."""
        svc = services(request)
        thread = await svc.threads.create(_new_id("thr"), _title(body.text))
        run = await svc.threads.start_turn(thread.id, build_request(thread.id, body))
        return RunAccepted(thread_id=thread.id, run_id=run.run_id)

    @app.get("/threads/{thread_id}", tags=["threads"])
    async def get_thread(request: Request, thread_id: str) -> ThreadDetail:
        return await services(request).threads.get(thread_id)

    @app.post("/threads/{thread_id}/messages", status_code=202, tags=["threads"])
    async def send_message(request: Request, thread_id: str, body: NewMessage) -> RunAccepted:
        """Add a turn. Returns 409 while the thread's previous run is still going."""
        run = await services(request).threads.start_turn(thread_id, build_request(thread_id, body))
        return RunAccepted(thread_id=thread_id, run_id=run.run_id)

    @app.get("/runs/{run_id}", tags=["runs"])
    async def get_run(request: Request, run_id: str) -> RunDetail:
        return await services(request).runs.get(run_id)

    @app.get("/runs/{run_id}/messages", tags=["runs"])
    async def get_run_messages(request: Request, run_id: str) -> list[Message]:
        """What the model saw: the run's history in its current epoch."""
        return await services(request).runs.messages(run_id)

    async def existing_run(request: Request, run_id: str) -> str:
        # A dependency runs before the response starts; a streaming route body
        # runs after, too late to answer 404.
        if not await services(request).runs.exists(run_id):
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return run_id

    @app.get("/runs/{run_id}/events", response_class=EventSourceResponse, tags=["runs"])
    async def stream_run_events(
        request: Request,
        run_id: Annotated[str, Depends(existing_run)],
        after: int = -1,
        last_event_id: Annotated[int | None, Header()] = None,
    ) -> AsyncIterable[RunEvent]:
        """Server-sent events for one run: stored events after ``after`` (or the
        ``Last-Event-ID`` header), then live events until the run stops."""
        svc = services(request)
        start = max(after, last_event_id if last_event_id is not None else -1)
        async for event in run_event_stream(svc.runs, svc.listener, run_id, after_seq=start):
            yield _frame(event)

    @app.get("/blobs/{blob_id}", tags=["runs"])
    async def get_blob(request: Request, blob_id: str) -> Blob:
        """The full text of a tool result that was truncated for the model."""
        return Blob(id=blob_id, content=await services(request).runs.blob(blob_id))

    return app
