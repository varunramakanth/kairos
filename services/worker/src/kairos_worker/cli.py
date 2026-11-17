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
from pathlib import Path
from typing import Annotated

import structlog
import typer

from kairos_db import RunQueue, create_engine
from kairos_db.migrate import upgrade
from kairos_domain.events import RunEvent
from kairos_domain.run import RunRequest
from kairos_harness import InMemoryCheckpointer, Provider
from kairos_harness.cassette import RecordingProvider, ReplayProvider
from kairos_worker.service import open_runtime, work
from kairos_worker.settings import Settings
from kairos_worker.wiring import build_provider

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = structlog.get_logger(__name__)


def _print_event(event: RunEvent) -> None:
    if event.type == "text_message_delta":
        sys.stdout.write(event.delta)
        sys.stdout.flush()
    elif event.type == "tool_call_end":
        sys.stdout.write(f"\n[tool] {event.name} {event.input}\n")
    elif event.type == "context_compacted":
        sys.stdout.write(f"\n[compacted {event.messages_summarized} messages]\n")
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
        async with open_runtime(settings, provider=provider) as runtime:
            loop = runtime.loop(InMemoryCheckpointer())
            request = RunRequest(
                run_id=f"run_{uuid.uuid4().hex}", input=prompt, model=model or settings.model
            )
            async for event in loop.run(request):
                _print_event(event)

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
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            async with open_runtime(settings, engine=engine) as runtime:
                await work(runtime, engine, stop)
        finally:
            await engine.dispose()

    asyncio.run(main())


if __name__ == "__main__":
    app()
