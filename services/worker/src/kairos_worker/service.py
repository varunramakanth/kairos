"""Process-level assembly shared by the CLI and the API's embedded worker."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version

import structlog
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import EventPublisher, JobStore, Lease, RunQueue
from kairos_db.governance import ApprovalStore, AuditStore, NotificationStore, SettingsStore
from kairos_db.memories import MemoryStore
from kairos_domain.governance import ApprovalStatus
from kairos_domain.jobs import JobStatus
from kairos_harness import Clock, HookChain, Hooks, Provider, RunContext, SystemClock
from kairos_memory.reflection import ReflectionHooks
from kairos_notify import (
    Channel,
    ConsoleChannel,
    Dispatcher,
    SesChannel,
    SmtpChannel,
    WebPushChannel,
)
from kairos_policy import PolicyHooks
from kairos_scheduler.scheduler import DIGEST_JOB_ID, Scheduler, digest_spec
from kairos_scheduler.timing import next_fire
from kairos_telemetry import TracingHooks, configure_tracing
from kairos_tools.mcp import open_toolsets
from kairos_worker.audit import AuditHooks
from kairos_worker.settings import Settings
from kairos_worker.wiring import (
    Runtime,
    add_delegate,
    build_provider,
    build_tools,
    http_client,
    mcp_configs,
)
from kairos_worker.worker import Worker

HARNESS_VERSION = version("kairos-harness")
log = structlog.get_logger(__name__)


def parse_headers(raw: str) -> dict[str, str]:
    """Parse ``key=value,key=value`` (the ``OTEL_EXPORTER_OTLP_HEADERS`` format)."""
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {key.strip(): value.strip() for key, value in pairs}


def tracing(settings: Settings, provider: Provider) -> tuple[TracerProvider, TracingHooks]:
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
    return tracer_provider, hooks


@asynccontextmanager
async def open_runtime(
    settings: Settings,
    *,
    provider: Provider | None = None,
    engine: AsyncEngine | None = None,
    clock: Clock | None = None,
) -> AsyncGenerator[Runtime]:
    """Provider, tools and tracing for this process, closed together on exit.

    With an ``engine`` the agent also gets the schedule tools.
    """
    provider = provider or build_provider(settings)
    tracer_provider, tracing_hooks = tracing(settings, provider)
    async with http_client() as http, AsyncExitStack() as stack:
        try:
            memories = MemoryStore(engine) if engine is not None else None
            if engine is None:
                tools = build_tools(settings, http, clock=clock)
            else:
                notifications = NotificationStore(engine)
                tools = build_tools(
                    settings,
                    http,
                    jobs=JobStore(engine),
                    notify=lambda n, key: notifications.enqueue(n, dedupe_key=key),
                    memory=memories,
                    clock=clock,
                )
            # MCP tools (the browser, configured servers) come after the built-ins, and
            # before the hooks, which read every tool's risk.
            for toolset in await open_toolsets(stack, mcp_configs(settings)):
                for item in toolset.tools:
                    tools.register(item)
            hooks: list[Hooks] = (
                [PolicyHooks()]
                if engine is None
                else [
                    PolicyHooks(actions_enabled=SettingsStore(engine).actions_enabled),
                    AuditHooks(AuditStore(engine), tools.specs()),
                ]
            )
            if memories is not None and settings.memory_reflection:
                hooks.append(ReflectionHooks(provider, memories, model=settings.job_model))
            # Policy first (its decision wins), tracing last (it sees the final state).
            chain = HookChain([*hooks, tracing_hooks])
            add_delegate(tools, settings, provider, hooks=chain, clock=clock)
            yield Runtime(settings, provider, tools, chain)
        finally:
            tracer_provider.shutdown()


def channels(settings: Settings, store: NotificationStore) -> list[Channel]:
    """Delivery channels that are configured. The console is always on."""
    configured: list[Channel] = [ConsoleChannel()]
    to, username, password = settings.email_to, settings.smtp_username, settings.smtp_password
    if settings.email_provider == "ses" and to:
        configured.append(
            SesChannel(
                recipient=to,
                sender=settings.email_from or to,
                region=settings.ses_region,
                profile=settings.ses_profile,
                base_url=settings.web_base_url,
            )
        )
    elif settings.email_provider == "smtp" and to and username and password:
        configured.append(
            SmtpChannel(
                recipient=to,
                username=username,
                password=password,
                host=settings.smtp_host,
                port=settings.smtp_port,
                base_url=settings.web_base_url,
            )
        )
    if settings.vapid_public_key and settings.vapid_private_key:
        configured.append(
            WebPushChannel(
                subscriptions=store.subscriptions,
                forget=store.forget,
                vapid_private_key=settings.vapid_private_key,
                subject=f"mailto:{settings.email_to or 'owner@localhost'}",
            )
        )
    return configured


async def ensure_digest_job(store: JobStore, settings: Settings, clock: Clock) -> None:
    """Create the weekly digest job once. Later edits (pause, new time) are kept."""
    spec = digest_spec(settings.timezone)
    now = clock.now()
    await store.create(
        DIGEST_JOB_ID,
        spec,
        status=JobStatus.ACTIVE,
        next_run_at=next_fire(spec.schedule, after=now),
        now=now,
    )


async def run_context(approvals: ApprovalStore, lease: Lease) -> RunContext:
    """Human decisions for a run that is being resumed."""
    decided = await approvals.for_run(lease.run_id)
    return RunContext(
        approved=frozenset(a.tool_call_id for a in decided if a.status is ApprovalStatus.APPROVED),
        denied={a.tool_call_id: a.note or "" for a in decided if a.status is ApprovalStatus.DENIED},
    )


async def work(
    runtime: Runtime,
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    clock: Clock | None = None,
) -> None:
    """Execute runs and fire scheduled jobs until ``stop`` is set.

    The run worker and the scheduler loop share the process. Several processes can
    run this side by side: leases keep runs and job firings exclusive.
    """
    settings = runtime.settings
    clock = clock or SystemClock()
    jobs = JobStore(engine)
    notifications = NotificationStore(engine)
    approvals = ApprovalStore(engine)
    await ensure_digest_job(jobs, settings, clock)
    scheduler = Scheduler(
        jobs,
        model=settings.job_model,
        clock=clock,
        lease_ttl_seconds=settings.lease_ttl_seconds,
        timezone=settings.timezone,
    )
    configured = channels(settings, notifications)
    dispatcher = Dispatcher(notifications, configured, clock=clock)
    async with EventPublisher(engine) as publisher:
        worker = Worker(
            RunQueue(engine),
            lambda lease: runtime.durable_loop(engine, lease),
            lease_ttl_seconds=settings.lease_ttl_seconds,
            poll_seconds=settings.poll_seconds,
            max_attempts=settings.max_attempts,
            on_event=publisher.publish,
            context_for=lambda lease: run_context(approvals, lease),
        )
        log.info(
            "worker_started",
            provider=runtime.provider.name,
            model=settings.model,
            channels=[c.name for c in configured],
        )
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.run_forever(stop))
            group.create_task(
                scheduler.run_forever(stop, poll_seconds=settings.scheduler_poll_seconds)
            )
            group.create_task(dispatcher.run_forever(stop))
        log.info("worker_stopped")
