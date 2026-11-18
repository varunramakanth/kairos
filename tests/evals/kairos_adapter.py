"""The Kairos harness as a ``kairos_evals`` adapter.

A golden case runs through the real agent loop with the production tool set, the
strict approval policy and content-capturing tracing, then comes back as the
OpenTelemetry trace the evaluator reads. Nothing here is mocked except time, the
job store (in memory) and planted fixture pages:

- ``live=True`` calls Claude and the web and records both into cassettes. It costs
  money; run it through ``just record-evals``.
- ``live=False`` replays those cassettes: offline, free and deterministic.

Each case gets a fixed clock (``Example.now``, default ``REFERENCE_NOW``) so relative
times such as "tomorrow at 8" mean the same thing on every replay.
"""

import json
import tempfile
from collections.abc import Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx2
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from kairos_db.threads import continuable_history
from kairos_domain.content import Message
from kairos_domain.events import RunFinished
from kairos_domain.governance import Notification
from kairos_domain.jobs import Job, JobSpec, JobStatus
from kairos_domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus
from kairos_domain.run import RunRequest
from kairos_evals import Example, OtlpJsonTraceSource, Trace, TraceFilter
from kairos_harness import AgentLoop, FakeClock, HookChain, InMemoryCheckpointer, SeededIdGenerator
from kairos_harness.cassette import (
    RecordingProvider,
    RecordingToolExecutor,
    ReplayProvider,
    ReplayToolExecutor,
)
from kairos_memory.testing import MemoryMemories
from kairos_policy import PolicyHooks
from kairos_scheduler.testing import MemoryJobs
from kairos_telemetry import OtlpJsonFileExporter, TracingHooks, configure_tracing

ROOT = Path(__file__).parent
CASSETTES = ROOT / "cassettes" / "m5"
REFERENCE_NOW = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
"""Wednesday 18 November 2026, 14:00 in Toronto."""
AGENT_MODEL = "claude-sonnet-5-5"
BROWSER_TAG = "browser"
MEMORY_TAG = "memory"
"""Cases with this tag get the memory tools, seeded from ``metadata.memories``."""
"""Cases with this tag also get the browser tools (live: the real browser service)."""


def _job(
    job_id: str, name: str, schedule: dict[str, Any], status: str = "active", **extra: Any
) -> Job:
    kind = "watch" if "watch" in extra else "task"
    spec = JobSpec.model_validate(
        {"name": name, "kind": kind, "schedule": schedule, **extra}
        | ({} if kind == "watch" else {"prompt": extra.get("prompt", name)})
    )
    return Job(
        id=job_id,
        spec=spec,
        status=JobStatus(status),
        next_run_at=None,
        last_run_at=datetime(2026, 11, 1, 14, 0, tzinfo=UTC) if job_id == "job_rent" else None,
        consecutive_failures=0,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _cron(expression: str) -> dict[str, Any]:
    return {"kind": "cron", "expression": expression, "timezone": "America/Toronto"}


STANDARD_JOBS: tuple[Job, ...] = (
    _job("job_morning_news", "Morning news digest", _cron("0 7 * * 1-5")),
    _job("job_weather", "Daily weather briefing", _cron("30 7 * * *")),
    _job("job_rent", "Rent reminder", _cron("0 9 1 * *")),
    _job(
        "job_gas_watch",
        "Gas price watch",
        {"kind": "interval", "every_seconds": 21600},
        watch={
            "target": "Toronto regular gas price per litre",
            "extract": "average price in cents per litre",
            "condition": {"kind": "below", "threshold": 140},
        },
    ),
    _job(
        "job_raptors_watch",
        "Raptors ticket price watch",
        _cron("0 10 * * *"),
        watch={
            "target": "Raptors home game tickets",
            "extract": "cheapest ticket price in CAD",
            "condition": {"kind": "below", "threshold": 120},
        },
    ),
    _job("job_monday_digest", "Monday news digest", _cron("0 8 * * 1"), status="paused"),
    _job("job_lunch_walk", "Lunch walk reminder", _cron("0 12 * * 1-5"), status="paused"),
)
"""The user's existing jobs in every case, so "pause my news digest" has a target."""


def seeded_memories(example: Example) -> MemoryMemories:
    """The user's confirmed memories for a case (``metadata.memories``)."""
    store = MemoryMemories()
    raw = example.metadata.get("memories")
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, dict):
            store.items[f"mem_seed{i}"] = Memory(
                id=f"mem_seed{i}",
                kind=MemoryKind(str(item.get("kind", "preference"))),
                content=str(item.get("content", "")),
                status=MemoryStatus.ACTIVE,
                source=MemorySource.USER,
                run_id=None,
                created_at=REFERENCE_NOW,
                updated_at=REFERENCE_NOW,
            )
    return store


def seeded_jobs() -> MemoryJobs:
    store = MemoryJobs()
    store.jobs = {job.id: job for job in STANDARD_JOBS}
    return store


class FixtureTransport(httpx2.AsyncBaseTransport):
    """Serves planted pages (for injection cases) and passes everything else on."""

    def __init__(self, pages: dict[str, str], inner: httpx2.AsyncBaseTransport) -> None:
        self._pages = pages
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        page = self._pages.get(str(request.url))
        if page is not None:
            return httpx2.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, text=page
            )
        return await self._inner.handle_async_request(request)


def fixture_pages(example: Example) -> dict[str, str]:
    pages = example.metadata.get("fixture_pages")
    if not isinstance(pages, dict):
        return {}
    return {str(url): str(html) for url, html in pages.items()}


class KairosAdapter:
    """Runs golden cases through ``AgentLoop``. One cassette pair per run id."""

    name = "kairos"

    def __init__(
        self, *, live: bool, model: str = AGENT_MODEL, cassettes: Path = CASSETTES
    ) -> None:
        self._live = live
        self._model = model
        self._cassettes = cassettes
        self._traces = Path(tempfile.mkdtemp(prefix="kairos-evals-")) / "traces.jsonl"
        self._tracing = configure_tracing(
            service_version=self.version,
            processors=[SimpleSpanProcessor(OtlpJsonFileExporter(self._traces))],
        )
        self.cost_usd = 0.0

    @property
    def version(self) -> str:
        return version("kairos-harness")

    @property
    def model(self) -> str:
        return self._model

    async def run(self, example: Example) -> Trace:
        trace, _ = await self.turn(example, run_id=example.id, text=example.input, history=())
        return trace

    async def conversation(self, example: Example) -> "KairosSession":
        return KairosSession(self, example)

    async def turn(
        self, example: Example, *, run_id: str, text: str, history: Sequence[Message]
    ) -> tuple[Trace, tuple[Message, ...]]:
        """One agent run. Returns its trace and the history for a following turn."""
        model_path = self._cassettes / f"{run_id}.jsonl"
        tool_path = self._cassettes / f"{run_id}.tools.jsonl"
        clock = FakeClock(example.now or REFERENCE_NOW)
        checkpointer = InMemoryCheckpointer()
        hooks = HookChain(
            [
                PolicyHooks(),
                TracingHooks(
                    self._tracing.get_tracer("kairos"),
                    provider_name="anthropic",
                    harness_version=self.version,
                    capture_content=True,
                ),
            ]
        )
        request = RunRequest(run_id=run_id, input=text, model=self._model, history=tuple(history))
        if self._live:
            finished = await self._record(
                example, request, model_path, tool_path, clock, hooks, checkpointer
            )
        else:
            loop = AgentLoop(
                provider=ReplayProvider(model_path, name="anthropic"),
                tools=ReplayToolExecutor(tool_path),
                checkpointer=checkpointer,
                clock=clock,
                ids=SeededIdGenerator(run_id),
                hooks=hooks,
            )
            finished = await _finish(loop, request)
        self.cost_usd += finished.cost_usd
        traces = [
            t
            async for t in OtlpJsonTraceSource(self._traces).load(
                TraceFilter(attribute_equals={"kairos.run_id": run_id})
            )
        ]
        if len(traces) != 1:
            raise RuntimeError(f"expected one trace for {run_id}, found {len(traces)}")
        state = await checkpointer.load(run_id)
        messages = state.messages if state else []
        return traces[0], continuable_history(messages)

    async def _record(
        self,
        example: Example,
        request: RunRequest,
        model_path: Path,
        tool_path: Path,
        clock: FakeClock,
        hooks: HookChain,
        checkpointer: InMemoryCheckpointer,
    ) -> RunFinished:
        # Imported here so replay never needs API keys or worker settings.
        from kairos_providers import anthropic_provider
        from kairos_tools.mcp import McpToolset
        from kairos_worker.settings import Settings
        from kairos_worker.wiring import BROWSER_SERVER, build_tools

        for path in (model_path, tool_path):
            path.unlink(missing_ok=True)  # a re-recording replaces the old one
        transport = FixtureTransport(fixture_pages(example), httpx2.AsyncHTTPTransport())
        async with (
            httpx2.AsyncClient(transport=transport, follow_redirects=False) as http,
            AsyncExitStack() as stack,
        ):
            memory = seeded_memories(example) if MEMORY_TAG in example.tags else None
            tools = build_tools(
                Settings(), http, jobs=seeded_jobs(), notify=_discard, memory=memory, clock=clock
            )
            if BROWSER_TAG in example.tags:
                # Only browser cases get the browser: adding its tools to every case would
                # change every recorded request. Planted pages are served by the browser.
                pages = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "pages.json"
                pages.write_text(json.dumps(fixture_pages(example)))
                config = BROWSER_SERVER.model_copy(
                    update={"env": {"KAIROS_BROWSER_FIXTURES": str(pages)}}
                )
                toolset = await stack.enter_async_context(McpToolset(config))
                for item in toolset.tools:
                    tools.register(item)
            loop = AgentLoop(
                provider=RecordingProvider(anthropic_provider(), model_path),
                tools=RecordingToolExecutor(tools, tool_path),
                checkpointer=checkpointer,
                clock=clock,
                ids=SeededIdGenerator(request.run_id),
                hooks=hooks,
            )
            return await _finish(loop, request)


class KairosSession:
    """A multi-turn conversation: each reply is a new run continuing the history,
    exactly as a chat thread does in the app."""

    def __init__(self, adapter: KairosAdapter, example: Example) -> None:
        self._adapter = adapter
        self._example = example
        self._history: tuple[Message, ...] = ()
        self._turns = 0

    async def reply(self, message: str) -> Trace:
        self._turns += 1
        trace, self._history = await self._adapter.turn(
            self._example,
            run_id=f"{self._example.id}.t{self._turns}",
            text=message,
            history=self._history,
        )
        return trace


async def _finish(loop: AgentLoop, request: RunRequest) -> RunFinished:
    events = [event async for event in loop.run(request)]
    return next(e for e in events if isinstance(e, RunFinished))


async def _discard(notification: Notification, dedupe_key: str) -> None:
    """``notify_me`` always waits for approval in evals, so nothing is ever sent."""


def replay(*, live: bool = False, **kwargs: Any) -> KairosAdapter:
    """CLI factory: ``--harness tests.evals.kairos_adapter:replay``."""
    return KairosAdapter(live=live, **kwargs)


def haiku(*, live: bool = False) -> KairosAdapter:
    """The same suites on Haiku 5.5, for the layer-3 model comparison."""
    return KairosAdapter(live=live, model="claude-haiku-5-5", cassettes=CASSETTES / "haiku")
