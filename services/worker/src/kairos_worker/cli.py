"""``kairos``: command-line entrypoint.

kairos migrate                         apply database migrations
kairos run "question"                  run once in memory, print the answer
kairos run "question" --replay FILE    same, served from a cassette (no network)
kairos enqueue "question"              queue a durable run for a worker
kairos worker                          claim and execute queued runs
"""

import asyncio
import signal
import sys
import uuid
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import structlog
import typer
from opentelemetry.sdk.trace import TracerProvider

from kairos_db import RunQueue, create_engine
from kairos_db.migrate import upgrade
from kairos_domain.events import RunEvent
from kairos_domain.run import RunRequest
from kairos_harness import HookChain, Hooks, InMemoryCheckpointer, Provider
from kairos_harness.cassette import RecordingProvider, ReplayProvider
from kairos_telemetry import TracingHooks, configure_tracing
from kairos_worker.settings import Settings
from kairos_worker.wiring import build_durable_loop, build_loop, build_provider
from kairos_worker.worker import Worker

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = structlog.get_logger(__name__)
HARNESS_VERSION = version("kairos-harness")


def _otlp_headers(raw: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {key.strip(): value.strip() for key, value in pairs}


def _tracing(settings: Settings, provider: Provider) -> tuple[TracerProvider, Hooks]:
    tracer_provider = configure_tracing(
        service_version=HARNESS_VERSION,
        otlp_endpoint=settings.otlp_endpoint,
        otlp_headers=_otlp_headers(settings.otlp_headers),
    )
    hooks = TracingHooks(
        tracer_provider.get_tracer("kairos"),
        provider_name=provider.name,
        harness_version=HARNESS_VERSION,
    )
    return tracer_provider, HookChain([hooks])


def _print_event(event: RunEvent) -> None:
    if event.type == "text_message_delta":
        sys.stdout.write(event.delta)
        sys.stdout.flush()
    elif event.type == "tool_call_end":
        sys.stdout.write(f"\n[tool] {event.name} {event.input}\n")
    elif event.type == "run_finished":
        detail = f" ({event.error})" if event.error else ""
        sys.stdout.write(
            f"\n\n[{event.stop_reason.value}{detail}] "
            f"tokens={event.usage.total_tokens} cost=${event.cost_usd:.4f}\n"
        )


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    upgrade(Settings().database_url)
    typer.echo("migrations applied")


@app.command()
def run(
    prompt: str,
    model: Annotated[str | None, typer.Option(help="Model ID; defaults to KAIROS_MODEL")] = None,
    replay: Annotated[Path | None, typer.Option(help="Serve model calls from a cassette")] = None,
    record: Annotated[Path | None, typer.Option(help="Record model calls to a cassette")] = None,
) -> None:
    """Run once in memory (no database) and stream the answer."""
    settings = Settings()

    async def main() -> None:
        provider: Provider = (
            ReplayProvider(replay, name="anthropic") if replay else build_provider(settings)
        )
        if record:
            provider = RecordingProvider(provider, record)
        tracer_provider, hooks = _tracing(settings, provider)
        loop = build_loop(provider, InMemoryCheckpointer(), hooks)
        request = RunRequest(
            run_id=f"run_{uuid.uuid4().hex}", input=prompt, model=model or settings.model
        )
        try:
            async for event in loop.run(request):
                _print_event(event)
        finally:
            tracer_provider.shutdown()

    asyncio.run(main())


@app.command()
def enqueue(
    prompt: str,
    model: Annotated[str | None, typer.Option(help="Model ID; defaults to KAIROS_MODEL")] = None,
) -> None:
    """Queue a durable run. A running ``kairos worker`` will pick it up."""
    settings = Settings()

    async def main() -> str:
        engine = create_engine(settings.database_url)
        run_id = f"run_{uuid.uuid4().hex}"
        try:
            request = RunRequest(run_id=run_id, input=prompt, model=model or settings.model)
            await RunQueue(engine).enqueue(request)
        finally:
            await engine.dispose()
        return run_id

    typer.echo(asyncio.run(main()))


@app.command()
def worker() -> None:
    """Claim and execute queued runs until interrupted (Ctrl-C or SIGTERM)."""
    settings = Settings()

    async def main() -> None:
        engine = create_engine(settings.database_url)
        provider = build_provider(settings)
        tracer_provider, hooks = _tracing(settings, provider)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        runner = Worker(
            RunQueue(engine),
            lambda lease: build_durable_loop(settings, engine, provider, lease, hooks),
            lease_ttl_seconds=settings.lease_ttl_seconds,
            poll_seconds=settings.poll_seconds,
            max_attempts=settings.max_attempts,
        )
        log.info("worker_started", provider=provider.name, model=settings.model)
        try:
            await runner.run_forever(stop)
        finally:
            tracer_provider.shutdown()
            await engine.dispose()
        log.info("worker_stopped")

    asyncio.run(main())


if __name__ == "__main__":
    app()
