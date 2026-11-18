"""Online evals: score every finished production run with reference-free metrics.

Offline suites compare a run with a labelled example. Production runs have no label,
so only metrics that judge a run on its own apply here: did it end healthily, did its
tools fail, did it repeat itself, what did it cost and how long did it take. They are
deterministic and free. (An LLM judge over a sample would need prompt content in the
spans, which production does not capture; it is a later, opt-in step.)

The worker's own spans are the source, so nothing is stored that tracing did not
already produce: a span processor collects each run's spans in memory, the root span
closes the trace, and the batch is scored and written as an ``online`` eval run that
the /evals dashboard shows next to the offline suites.
"""

import asyncio
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime

import structlog
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from kairos_evals import EvalRun, Evaluator, Metric, ResultSink, Score, Trace, traces_from_otlp
from kairos_evals.metrics import CostPerTask, LatencyPerTask, PlanCompletion, ToolErrorRate
from kairos_evals.stats import summarize_results
from kairos_evals.types import Example, Kind, Layer, not_applicable
from kairos_telemetry.otlp_json import spans_to_otlp_json

SUITE = "online"
log = structlog.get_logger(__name__)


class RunHealth:
    """The run ended the way a working run ends in production.

    Unlike the offline ``task_completion``, a pause for an approval or an answer is
    healthy here: that is the agent asking, as it should. Failures, refusals and
    budget stops are not.
    """

    id = "run_health"
    layer: Layer = "outcome"
    kind: Kind = "deterministic"
    HEALTHY = frozenset({"completed", "awaiting_approval", "awaiting_input", "cancelled"})

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        got = trace.stop_reason
        if got is None:
            return not_applicable("the trace records no stop reason")
        ok = got in self.HEALTHY
        return Score(value=1.0 if ok else 0.0, passed=ok, reason=f"stopped {got}")


def online_metrics() -> list[Metric]:
    """The metrics that need no label. Cost and latency are measured, not gated."""
    return [RunHealth(), ToolErrorRate(), PlanCompletion(), CostPerTask(), LatencyPerTask()]


class TraceCollector(SpanExporter):
    """Groups spans by trace and hands over a ``Trace`` when its root span ends.

    Use it with a ``SimpleSpanProcessor``: export then happens on the event loop as
    each span ends, so no locking is needed. Children end before their root, so a
    trace is complete when the root arrives. Traces whose root never ends (a crashed
    run) are dropped oldest first beyond ``max_open``.
    """

    def __init__(self, on_trace: Callable[[Trace], None], *, max_open: int = 200) -> None:
        self._on_trace = on_trace
        self._max_open = max_open
        self._open: OrderedDict[int, list[ReadableSpan]] = OrderedDict()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            if span.context is None:
                continue
            group = self._open.setdefault(span.context.trace_id, [])
            group.append(span)
            if span.parent is None:
                del self._open[span.context.trace_id]
                self._finish(group)
        while len(self._open) > self._max_open:
            self._open.popitem(last=False)
        return SpanExportResult.SUCCESS

    def _finish(self, spans: list[ReadableSpan]) -> None:
        try:
            for trace in traces_from_otlp([spans_to_otlp_json(spans)]):
                self._on_trace(trace)
        except Exception as error:  # evals must never break tracing or the run
            log.warning("online_eval_trace_failed", error=str(error))

    def shutdown(self) -> None:
        self._open.clear()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class OnlineEvals:
    """Collects finished runs and periodically writes their scores to ``sink``."""

    def __init__(
        self,
        sink: ResultSink,
        *,
        now: Callable[[], datetime],
        harness_version: str,
        model: str,
        metrics: Sequence[Metric] | None = None,
        max_pending: int = 1000,
    ) -> None:
        self._sink = sink
        self._now = now
        self._harness_version = harness_version
        self._model = model
        self._evaluator = Evaluator(metrics if metrics is not None else online_metrics())
        self._pending: deque[Trace] = deque(maxlen=max_pending)
        self._window_start = now()
        self.collector = TraceCollector(self.collect)

    def collect(self, trace: Trace) -> None:
        """Queue one finished run. Only agent runs count, not stray spans."""
        if trace.root is not None and trace.root.operation == "invoke_agent":
            self._pending.append(trace)

    async def flush(self) -> EvalRun | None:
        """Score and write what has been collected since the last flush."""
        if not self._pending:
            return None
        traces = list(self._pending)
        started, finished = self._window_start, self._now()
        results = await self._evaluator.evaluate((trace, None) for trace in traces)
        run = EvalRun(
            id=f"online-{uuid.uuid4().hex[:12]}",
            suite=SUITE,
            harness="kairos",
            harness_version=self._harness_version,
            model=self._model,
            started_at=started,
            finished_at=finished,
            examples=len(traces),
            summary=summarize_results(results),
        )
        await self._sink.write(run, results)
        # Only now drop what was written; runs that finished meanwhile stay queued.
        for _ in traces:
            self._pending.popleft()
        self._window_start = finished
        log.info("online_evals_written", runs=len(traces), eval_run=run.id)
        return run

    async def run_forever(self, stop: asyncio.Event, *, flush_seconds: float) -> None:
        """Flush every ``flush_seconds`` and once more on shutdown. A failed write is
        logged and retried at the next flush (the queue keeps the newest runs)."""
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=flush_seconds)
            try:
                await self.flush()
            except Exception as error:
                log.warning("online_evals_failed", error=str(error))
