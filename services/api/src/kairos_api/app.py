"""The HTTP API: threads, runs and the typed run event stream (ADR-002).

Every request and response body is a Pydantic model, so ``/openapi.json`` describes
the whole surface, including the SSE event union, and the web UI's TypeScript
client is generated from it (``just gen-client``).

Run it with ``just api`` (which also starts an embedded worker) or
``uvicorn --factory kairos_api.app:create_app``.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_api.auth import Authenticator, StripPrefix
from kairos_api.limits import RateLimiter
from kairos_api.settings import ApiSettings
from kairos_api.setup import SetupStatus, setup_status
from kairos_api.streams import run_event_stream
from kairos_db import (
    EventListener,
    JobStore,
    NotFoundError,
    RunReader,
    ThreadBusyError,
    ThreadRepository,
    create_engine,
)
from kairos_db.evals import EvalStore
from kairos_db.governance import (
    AlreadyDecidedError,
    ApprovalStore,
    AuditStore,
    NotificationStore,
    SettingsStore,
)
from kairos_db.memories import MemoryStore
from kairos_domain.base import DomainModel
from kairos_domain.content import Message
from kairos_domain.evals import (
    CriterionCalibration,
    EvalRunDetail,
    EvalRunSummary,
    ReviewItem,
    ReviewLabel,
)
from kairos_domain.events import RunEvent
from kairos_domain.governance import (
    Approval,
    ApprovalStatus,
    AuditEntry,
    ChannelName,
    Notification,
    NotificationKind,
    PushSubscription,
    StoredNotification,
)
from kairos_domain.jobs import Job, JobDetail, JobKind, JobSpec, JobStatus, JobSummary
from kairos_domain.memory import Memory, MemoryCreate, MemorySource, MemoryStatus, MemoryUpdate
from kairos_domain.run import RunRequest
from kairos_domain.threads import RunDetail, Thread, ThreadDetail
from kairos_evals.judge import CRITERIA
from kairos_evals.simulator import CONVERSATION_CRITERIA
from kairos_evals.stats import cohens_kappa
from kairos_harness import Provider, SystemClock
from kairos_scheduler.timing import ScheduleError, next_fire, validate
from kairos_worker.service import open_runtime, spend_cap, work
from kairos_worker.spend import SpendCap
from kairos_worker.wiring import memory_store

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


class JobPatch(DomainModel):
    """Change a job's definition, its status, or both."""

    spec: JobSpec | None = None
    status: Literal["active", "paused", "deleted"] | None = None


class PausedJobs(DomainModel):
    paused: int


class Decision(DomainModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)
    """Shown to the model when declining (e.g. "send it tomorrow instead")."""


class AuthConfig(DomainModel):
    enabled: bool
    issuer: str | None = None
    client_id: str | None = None
    domain: str | None = None
    """Cognito Hosted UI base URL (sign-in and sign-out pages)."""


MIN_LABELS = 10
MIN_KAPPA = 0.6
JUDGE_QUESTIONS: dict[str, str] = {
    **CRITERIA,
    **CONVERSATION_CRITERIA,
    "recovery": "After a tool failed, did the assistant recover sensibly or say plainly "
    "what failed, without presenting results it did not get?",
}
"""The question each judge criterion answers, shown to the human reviewer."""


class AppSettings(DomainModel):
    actions_enabled: bool
    """False while the kill switch is on: every agent action with a side effect is denied."""
    email_to: str | None
    email_configured: bool
    push_configured: bool
    vapid_public_key: str | None
    timezone: str
    spent_today_usd: float
    """Model spend so far today (local day), across all runs."""
    daily_spend_cap_usd: float | None


class KillSwitch(DomainModel):
    actions_enabled: bool


class Queued(DomainModel):
    channels: list[ChannelName]


class Unsubscribe(DomainModel):
    endpoint: str


class TestNotification(DomainModel):
    channels: list[ChannelName] = Field(default_factory=lambda: ["console", "push"])
    """Email is opt-in for tests so nothing reaches the inbox by accident."""


# --- application ----------------------------------------------------------------------


@dataclass
class Services:
    engine: AsyncEngine
    threads: ThreadRepository
    runs: RunReader
    listener: EventListener
    jobs: JobStore
    approvals: ApprovalStore
    audit: AuditStore
    app_settings: SettingsStore
    notifications: NotificationStore
    evals: EvalStore
    memories: MemoryStore
    spend: SpendCap


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
            jobs=JobStore(engine),
            approvals=ApprovalStore(engine),
            audit=AuditStore(engine),
            app_settings=SettingsStore(engine),
            notifications=NotificationStore(engine),
            evals=EvalStore(engine),
            memories=memory_store(settings, engine),
            spend=spend_cap(settings, engine, SystemClock()),
        )
        stop = asyncio.Event()
        async with AsyncExitStack() as stack:
            stack.push_async_callback(engine.dispose)
            stack.push_async_callback(listener.close)
            if settings.embedded_worker:
                provider = provider_factory() if provider_factory else None
                runtime = await stack.enter_async_context(
                    open_runtime(settings, provider=provider, engine=engine)
                )
                task = asyncio.create_task(work(runtime, engine, stop))

                async def stop_worker() -> None:
                    stop.set()
                    await task

                stack.push_async_callback(stop_worker)
            yield

    authenticator = (
        Authenticator(issuer=settings.auth_issuer, client_id=settings.auth_client_id)
        if settings.auth_issuer and settings.auth_client_id
        else None
    )
    app = FastAPI(
        title="Kairos API",
        version=API_VERSION,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
        dependencies=[Depends(authenticator)] if authenticator else [],
    )
    if settings.path_prefix:
        app.add_middleware(StripPrefix, prefix=settings.path_prefix)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID", "Authorization"],
    )

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ScheduleError)
    async def bad_schedule(request: Request, error: ScheduleError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(AlreadyDecidedError)
    async def already_decided(request: Request, error: AlreadyDecidedError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

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

    @app.get("/auth/config", tags=["meta"])
    async def auth_config() -> AuthConfig:
        """What the web app needs to sign in. Public: it contains no secrets."""
        if authenticator is None or not settings.auth_domain:
            return AuthConfig(enabled=False)
        return AuthConfig(
            enabled=True,
            issuer=authenticator.issuer,
            client_id=authenticator.client_id,
            domain=settings.auth_domain.rstrip("/"),
        )

    @app.get("/health", tags=["meta"])
    async def health() -> Health:
        return Health(status="ok", version=API_VERSION)

    @app.get("/threads", tags=["threads"])
    async def list_threads(request: Request) -> list[Thread]:
        return await services(request).threads.recent()

    limiter = RateLimiter(limit=settings.rate_limit_per_minute, per_seconds=60.0)

    def rate_limited(request: Request) -> None:
        """Dependency: at most ``rate_limit_per_minute`` work-starting calls per client."""
        client = request.client.host if request.client else "unknown"
        if (wait := limiter.check(client)) is not None:
            raise HTTPException(
                status_code=429,
                detail="too many requests; slow down",
                headers={"Retry-After": str(max(1, round(wait)))},
            )

    async def admit_run(request: Request) -> None:
        """Dependency for anything that starts a model run: rate limit and spend cap."""
        rate_limited(request)
        if reason := await services(request).spend.blocked():
            raise HTTPException(status_code=429, detail=reason)

    @app.post("/threads", status_code=201, tags=["threads"], dependencies=[Depends(admit_run)])
    async def create_thread(request: Request, body: NewMessage) -> RunAccepted:
        """Start a conversation with its first message."""
        svc = services(request)
        thread = await svc.threads.create(_new_id("thr"), _title(body.text))
        run = await svc.threads.start_turn(thread.id, build_request(thread.id, body))
        return RunAccepted(thread_id=thread.id, run_id=run.run_id)

    @app.get("/threads/{thread_id}", tags=["threads"])
    async def get_thread(request: Request, thread_id: str) -> ThreadDetail:
        return await services(request).threads.get(thread_id)

    @app.post(
        "/threads/{thread_id}/messages",
        status_code=202,
        tags=["threads"],
        dependencies=[Depends(admit_run)],
    )
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

    # --- jobs -----------------------------------------------------------------------

    clock = SystemClock()

    async def job_or_404(request: Request, job_id: str) -> Job:
        job = await services(request).jobs.get(job_id)
        if job is None or job.status is JobStatus.DELETED:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        return job

    def first_fire(spec: JobSpec) -> datetime | None:
        now = clock.now()
        validate(spec.schedule, now=now)
        return next_fire(spec.schedule, after=now)

    def check_spec(spec: JobSpec) -> None:
        if spec.kind is JobKind.TASK and not spec.prompt:
            raise HTTPException(status_code=422, detail="a task needs a prompt")
        if spec.kind is JobKind.WATCH and spec.watch is None:
            raise HTTPException(status_code=422, detail="a watch needs a watch spec")

    @app.get("/jobs", tags=["jobs"])
    async def list_jobs(request: Request) -> list[JobSummary]:
        return await services(request).jobs.summaries()

    @app.post("/jobs", status_code=201, tags=["jobs"])
    async def create_job(request: Request, body: JobSpec) -> Job:
        """Create a job from the UI. It is active at once: the user is the one asking."""
        check_spec(body)
        return await services(request).jobs.create(
            _new_id("job"),
            body,
            status=JobStatus.ACTIVE,
            next_run_at=first_fire(body),
            now=clock.now(),
        )

    @app.post("/jobs/pause-all", tags=["jobs"])
    async def pause_all_jobs(request: Request) -> PausedJobs:
        """The kill switch: pause every active job."""
        svc = services(request)
        paused = await svc.jobs.pause_all(now=clock.now())
        await svc.audit.record(
            actor="user", action="job.paused_all", subject="jobs", detail={"count": paused}
        )
        return PausedJobs(paused=paused)

    @app.get("/jobs/{job_id}", tags=["jobs"])
    async def get_job(request: Request, job_id: str) -> JobDetail:
        await job_or_404(request, job_id)
        detail = await services(request).jobs.detail(job_id)
        assert detail is not None  # noqa: S101 (checked above)
        return detail

    @app.patch("/jobs/{job_id}", tags=["jobs"])
    async def update_job(request: Request, job_id: str, body: JobPatch) -> Job:
        job = await job_or_404(request, job_id)
        store = services(request).jobs
        if body.spec is not None:
            check_spec(body.spec)
            updated = await store.update_spec(
                job_id, body.spec, next_run_at=first_fire(body.spec), now=clock.now()
            )
            job = updated or job
        if body.status is not None:
            target = JobStatus(body.status)
            next_run = first_fire(job.spec) if target is JobStatus.ACTIVE else job.next_run_at
            updated = await store.set_status(job_id, target, next_run_at=next_run, now=clock.now())
            job = updated or job
        await services(request).audit.record(
            actor="user",
            action=f"job.{body.status}" if body.status else "job.edited",
            subject=job_id,
            detail={"spec_changed": body.spec is not None},
        )
        return job

    @app.post("/jobs/{job_id}/confirm", tags=["jobs"])
    async def confirm_job(request: Request, job_id: str) -> Job:
        """Activate a job the agent proposed."""
        job = await job_or_404(request, job_id)
        if job.status is not JobStatus.PENDING_CONFIRMATION:
            raise HTTPException(status_code=409, detail=f"job {job_id} is {job.status.value}")
        svc = services(request)
        updated = await svc.jobs.set_status(
            job_id, JobStatus.ACTIVE, next_run_at=first_fire(job.spec), now=clock.now()
        )
        assert updated is not None  # noqa: S101 (exists)
        await svc.audit.record(actor="user", action="job.confirmed", subject=job_id)
        return updated

    @app.post("/jobs/{job_id}/run", tags=["jobs"], dependencies=[Depends(admit_run)])
    async def run_job_now(request: Request, job_id: str) -> Job:
        """Fire an active job at the next scheduler tick instead of waiting."""
        job = await job_or_404(request, job_id)
        if job.status is not JobStatus.ACTIVE:
            raise HTTPException(status_code=409, detail=f"job {job_id} is {job.status.value}")
        updated = await services(request).jobs.run_now(job_id, now=clock.now())
        assert updated is not None  # noqa: S101 (exists)
        return updated

    # --- approvals, audit, settings, notifications (M4) --------------------------------

    @app.get("/approvals", tags=["approvals"])
    async def list_approvals(
        request: Request, status: ApprovalStatus | None = None
    ) -> list[Approval]:
        return await services(request).approvals.list_approvals(status=status)

    @app.get("/runs/{run_id}/approvals", tags=["approvals"])
    async def run_approvals(request: Request, run_id: str) -> list[Approval]:
        return await services(request).approvals.for_run(run_id)

    @app.post("/approvals/{approval_id}/decide", tags=["approvals"])
    async def decide_approval(request: Request, approval_id: str, body: Decision) -> Approval:
        """Approve or decline. When nothing else is pending for the run, it resumes."""
        try:
            return await services(request).approvals.decide(
                approval_id, approve=body.approve, note=body.note, now=clock.now()
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail=f"approval {approval_id} not found"
            ) from error

    @app.get("/audit", tags=["audit"])
    async def list_audit(
        request: Request, before: int | None = None, limit: Annotated[int, Query(le=500)] = 200
    ) -> list[AuditEntry]:
        return await services(request).audit.recent(limit=limit, before_id=before)

    @app.get("/memories", tags=["memory"])
    async def list_memories(
        request: Request,
        status: MemoryStatus | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> list[Memory]:
        """Newest first; with ``q``, the active memories that match it, best first."""
        store = services(request).memories
        if q:
            return list(await store.search(q, limit=limit))
        return list(await store.recent(status=status, limit=limit))

    @app.post("/memories", status_code=201, tags=["memory"])
    async def create_memory(request: Request, body: MemoryCreate) -> Memory:
        """A memory written by the user: active at once."""
        memory = await services(request).memories.create(
            body.content, body.kind, source=MemorySource.USER, status=MemoryStatus.ACTIVE
        )
        await services(request).audit.record(
            actor="user", action="memory.created", subject=memory.id
        )
        return memory

    @app.patch("/memories/{memory_id}", tags=["memory"])
    async def update_memory(request: Request, memory_id: str, body: MemoryUpdate) -> Memory:
        """Edit or re-classify a memory, or confirm a proposal (``status: active``)."""
        before = await services(request).memories.get(memory_id)
        memory = await services(request).memories.update(memory_id, body)
        if memory is None or before is None:
            raise HTTPException(status_code=404, detail=f"memory {memory_id} not found")
        confirmed = before.status is MemoryStatus.PROPOSED and memory.status is MemoryStatus.ACTIVE
        await services(request).audit.record(
            actor="user",
            action="memory.confirmed" if confirmed else "memory.updated",
            subject=memory_id,
            run_id=memory.run_id,
        )
        return memory

    @app.delete("/memories/{memory_id}", status_code=204, tags=["memory"])
    async def delete_memory(request: Request, memory_id: str) -> None:
        if not await services(request).memories.delete(memory_id):
            raise HTTPException(status_code=404, detail=f"memory {memory_id} not found")
        await services(request).audit.record(
            actor="user", action="memory.deleted", subject=memory_id
        )

    @app.get("/evals/review", tags=["evals"])
    async def review_queue(
        request: Request, limit: Annotated[int, Query(ge=1, le=50)] = 10
    ) -> list[ReviewItem]:
        """Judge verdicts awaiting the owner's label (judge calibration)."""
        items = await services(request).evals.review_queue(limit=limit)
        return [
            item.model_copy(
                update={"criterion": JUDGE_QUESTIONS.get(item.metric_id, item.metric_id)}
            )
            for item in items
        ]

    @app.post("/evals/review", status_code=204, tags=["evals"])
    async def label_verdict(request: Request, body: ReviewLabel) -> None:
        if not await services(request).evals.label(body):
            raise HTTPException(status_code=404, detail="no such judge verdict")

    @app.get("/evals/calibration", tags=["evals"])
    async def judge_calibration(request: Request) -> list[CriterionCalibration]:
        """Judge-human agreement per criterion. A criterion is trusted only with enough
        labels and a kappa of at least MIN_KAPPA."""
        report: list[CriterionCalibration] = []
        for metric_id, pairs in sorted((await services(request).evals.label_pairs()).items()):
            judge, human = [j for j, _ in pairs], [h for _, h in pairs]
            agreement = sum(j == h for j, h in pairs) / len(pairs)
            informative = len(set(judge) | set(human)) > 1
            kappa = cohens_kappa(judge, human) if informative else None
            report.append(
                CriterionCalibration(
                    metric_id=metric_id,
                    n=len(pairs),
                    agreement=round(agreement, 3),
                    kappa=round(kappa, 3) if kappa is not None else None,
                    trusted=len(pairs) >= MIN_LABELS and kappa is not None and kappa >= MIN_KAPPA,
                )
            )
        return report

    @app.get("/evals/runs", tags=["evals"])
    async def list_eval_runs(
        request: Request,
        suite: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[EvalRunSummary]:
        """Eval runs written by ``kairos-evals run --out postgres:...``, newest first."""
        return await services(request).evals.runs(suite=suite, limit=limit)

    @app.get("/evals/runs/{eval_run_id}", tags=["evals"])
    async def get_eval_run(request: Request, eval_run_id: str) -> EvalRunDetail:
        detail = await services(request).evals.detail(eval_run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"eval run {eval_run_id} not found")
        return detail

    async def current_settings(request: Request) -> AppSettings:
        return AppSettings(
            actions_enabled=await services(request).app_settings.actions_enabled(),
            email_to=settings.email_to,
            email_configured=settings.email_configured,
            push_configured=settings.push_configured,
            vapid_public_key=settings.vapid_public_key,
            timezone=settings.timezone,
            spent_today_usd=round(await services(request).spend.ledger.spent_today(clock.now()), 4),
            daily_spend_cap_usd=settings.daily_spend_cap_usd,
        )

    @app.get("/settings", tags=["settings"])
    async def get_settings(request: Request) -> AppSettings:
        return await current_settings(request)

    @app.get("/setup/status", tags=["settings"])
    async def get_setup_status() -> SetupStatus:
        """Which features are configured, for the Welcome checklist. Presence only."""
        return setup_status(settings, os.environ)

    @app.put("/settings/kill-switch", tags=["settings"])
    async def set_kill_switch(request: Request, body: KillSwitch) -> AppSettings:
        """``actions_enabled=false`` denies every agent action with a side effect and
        pauses all active jobs."""
        await services(request).app_settings.set_actions_enabled(
            body.actions_enabled, now=clock.now()
        )
        return await current_settings(request)

    @app.post("/push/subscriptions", status_code=201, tags=["notifications"])
    async def subscribe_push(request: Request, body: PushSubscription) -> AppSettings:
        await services(request).notifications.subscribe(body)
        return await current_settings(request)

    @app.post("/push/unsubscribe", tags=["notifications"])
    async def unsubscribe_push(request: Request, body: Unsubscribe) -> AppSettings:
        await services(request).notifications.forget(body.endpoint)
        return await current_settings(request)

    @app.get("/notifications", tags=["notifications"])
    async def list_notifications(request: Request) -> list[StoredNotification]:
        return await services(request).notifications.recent()

    @app.post(
        "/notifications/test",
        status_code=202,
        tags=["notifications"],
        dependencies=[Depends(rate_limited)],
    )
    async def send_test_notification(request: Request, body: TestNotification) -> Queued:
        """Queue a test message on the chosen channels (console and push by default)."""
        await services(request).notifications.enqueue(
            Notification(
                kind=NotificationKind.TEST,
                title="Test notification",
                body="If you can read this, Kairos can reach you here.",
                url="/settings",
                channels=body.channels,
            )
        )
        return Queued(channels=body.channels)

    return app
