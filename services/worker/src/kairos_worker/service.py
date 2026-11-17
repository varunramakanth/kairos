"""Process-level assembly shared by the CLI and the API's embedded worker."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version

import structlog
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import EventPublisher, RunQueue
from kairos_harness import HookChain, Hooks, Provider
from kairos_telemetry import TracingHooks, configure_tracing
from kairos_worker.settings import Settings
from kairos_worker.wiring import Runtime, build_provider, build_tools, http_client
from kairos_worker.worker import Worker

HARNESS_VERSION = version("kairos-harness")
log = structlog.get_logger(__name__)


def parse_headers(raw: str) -> dict[str, str]:
    """Parse ``key=value,key=value`` (the ``OTEL_EXPORTER_OTLP_HEADERS`` format)."""
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {key.strip(): value.strip() for key, value in pairs}


def tracing(settings: Settings, provider: Provider) -> tuple[TracerProvider, Hooks]:
    tracer_provider = configure_tracing(
        service_version=HARNESS_VERSION,
        otlp_endpoint=settings.otlp_endpoint,
        otlp_headers=parse_headers(settings.otlp_headers),
    )
    hooks = TracingHooks(
        tracer_provider.get_tracer("kairos"),
        provider_name=provider.name,
        harness_version=HARNESS_VERSION,
    )
    return tracer_provider, HookChain([hooks])


@asynccontextmanager
async def open_runtime(
    settings: Settings, *, provider: Provider | None = None
) -> AsyncGenerator[Runtime]:
    """Provider, tools and tracing for this process, closed together on exit."""
    provider = provider or build_provider(settings)
    tracer_provider, hooks = tracing(settings, provider)
    async with http_client() as http:
        try:
            yield Runtime(settings, provider, build_tools(settings, http), hooks)
        finally:
            tracer_provider.shutdown()


async def work(runtime: Runtime, engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Claim and execute runs until ``stop`` is set, publishing live events."""
    settings = runtime.settings
    async with EventPublisher(engine) as publisher:
        worker = Worker(
            RunQueue(engine),
            lambda lease: runtime.durable_loop(engine, lease),
            lease_ttl_seconds=settings.lease_ttl_seconds,
            poll_seconds=settings.poll_seconds,
            max_attempts=settings.max_attempts,
            on_event=publisher.publish,
        )
        log.info("worker_started", provider=runtime.provider.name, model=settings.model)
        await worker.run_forever(stop)
        log.info("worker_stopped")
