"""Online evals score real loop runs from their spans, with no labels and no network."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from kairos_domain.run import RunRequest, ToolCallRecord
from kairos_domain.tools import ToolContext
from kairos_evals import EvalResult, EvalRun
from kairos_harness import AgentLoop, FakeClock, HookChain, InMemoryCheckpointer
from kairos_harness.testing import (
    ScriptedProvider,
    StaticToolExecutor,
    Turn,
    simple_spec,
    text_turn,
    tool_turn,
)
from kairos_telemetry import TracingHooks, configure_tracing
from kairos_worker.online import SUITE, OnlineEvals, RunHealth

START = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)


class MemorySink:
    def __init__(self, *, fail: int = 0) -> None:
        self.written: list[tuple[EvalRun, list[EvalResult]]] = []
        self._fail = fail

    async def write(self, run: EvalRun, results: Sequence[EvalResult]) -> None:
        if self._fail:
            self._fail -= 1
            raise OSError("database unavailable")
        self.written.append((run, list(results)))


async def lookup(call: ToolCallRecord, context: ToolContext) -> str:
    return "42"


async def broken(call: ToolCallRecord, context: ToolContext) -> str:
    raise RuntimeError("upstream down")


async def run_once(online: OnlineEvals, run_id: str, turns: Sequence[Turn]) -> None:
    provider = configure_tracing(
        service_version="test", processors=[SimpleSpanProcessor(online.collector)]
    )
    tracing = TracingHooks(
        provider.get_tracer("kairos"), provider_name="anthropic", harness_version="test"
    )
    loop = AgentLoop(
        provider=ScriptedProvider(turns),
        tools=StaticToolExecutor(
            [(simple_spec("lookup"), lookup), (simple_spec("broken"), broken)]
        ),
        checkpointer=InMemoryCheckpointer(),
        hooks=HookChain([tracing]),
    )
    _ = [e async for e in loop.run(RunRequest(run_id=run_id, input="hi", model="m"))]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


def online_evals(sink: MemorySink, clock: FakeClock) -> OnlineEvals:
    return OnlineEvals(sink, now=clock.now, harness_version="test", model="claude-sonnet-5-5")


def scores(results: list[EvalResult], metric: str) -> list[float]:
    return [r.score.value for r in results if r.metric_id == metric and r.score.applicable]


async def test_each_finished_run_is_scored_without_a_label(clock: FakeClock) -> None:
    sink = MemorySink()
    online = online_evals(sink, clock)
    await run_once(online, "run_ok", [tool_turn(("t1", "lookup", {})), text_turn("done")])
    await run_once(online, "run_err", [tool_turn(("t2", "broken", {})), text_turn("sorry")])
    clock.advance(6 * 3600)

    run = await online.flush()

    assert run is not None
    assert run.suite == SUITE
    assert run.examples == 2
    assert (run.started_at, run.finished_at) == (START, START + timedelta(hours=6))
    ((written, results),) = sink.written
    assert written.id == run.id
    assert scores(results, "run_health") == [1.0, 1.0]
    assert sorted(scores(results, "tool_error_rate")) == [0.0, 1.0]
    assert {r.example_id for r in results} == {None}
    summary = {s.metric_id: s for s in run.summary}
    assert summary["tool_error_rate"].pass_rate == 0.5
    assert summary["cost_per_task"].n == 2


async def test_nothing_is_written_when_no_run_finished(clock: FakeClock) -> None:
    sink = MemorySink()
    assert await online_evals(sink, clock).flush() is None
    assert sink.written == []


async def test_a_failed_write_keeps_the_runs_for_the_next_flush(clock: FakeClock) -> None:
    sink = MemorySink(fail=1)
    online = online_evals(sink, clock)
    await run_once(online, "run_1", [text_turn("hello")])

    with pytest.raises(OSError, match="unavailable"):
        await online.flush()
    await run_once(online, "run_2", [text_turn("again")])
    run = await online.flush()

    assert run is not None
    assert run.examples == 2
    assert run.started_at == START  # the window covers the failed attempt too
    assert await online.flush() is None


async def test_run_health_counts_pauses_as_healthy() -> None:
    from kairos_evals import Span, Trace

    def trace(stop: str) -> Trace:
        root = Span(
            trace_id="t",
            span_id="s",
            name="invoke_agent",
            start=START,
            end=START,
            attributes={"gen_ai.operation.name": "invoke_agent", "kairos.stop_reason": stop},
        )
        return Trace(trace_id="t", spans=(root,))

    metric = RunHealth()
    for stop in ("completed", "awaiting_approval", "awaiting_input"):
        assert (await metric.evaluate(trace(stop))).passed
    for stop in ("failed", "refused", "budget_exceeded"):
        assert not (await metric.evaluate(trace(stop))).passed
