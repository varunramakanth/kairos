"""Layer 1 (final response / outcome): task completion, cost and latency per task.

Cost and latency carry the raw quantity in ``Score.measurement``, so a suite summary
reports p50 and p95 alongside the pass rate against an optional cap.
"""

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score, not_applicable


class TaskCompletion:
    """The run ended the way a correct run ends: ``completed`` by default, or the
    example's ``expected_stop_reason`` (``awaiting_approval`` for a gated action)."""

    id = "task_completion"
    layer: Layer = "outcome"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        expected = (example.expected_stop_reason if example else None) or "completed"
        got = trace.stop_reason
        if got is None:
            return not_applicable("the trace records no stop reason")
        ok = got == expected
        return Score(
            value=1.0 if ok else 0.0, passed=ok, reason=f"stopped {got}, expected {expected}"
        )


class CostPerTask:
    """Dollars per run. Passes under ``max_usd`` (or the example's ``cost_usd`` cap)."""

    id = "cost_per_task"
    layer: Layer = "outcome"
    kind: Kind = "deterministic"

    def __init__(self, max_usd: float | None = None) -> None:
        self._max = max_usd

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        cost = trace.cost_usd
        cap = (example.budget.get("cost_usd") if example else None) or self._max
        ok = cap is None or cost <= cap
        return Score(
            value=1.0 if ok else 0.0,
            passed=ok,
            reason=f"${cost:.4f}" + (f" (cap ${cap:.2f})" if cap is not None else ""),
            measurement=cost,
        )


class LatencyPerTask:
    """Wall-clock seconds from the first span to the last. In replayed runs this is
    the replay time, not the original latency, so gate on it only for live runs."""

    id = "latency_per_task"
    layer: Layer = "outcome"
    kind: Kind = "deterministic"

    def __init__(self, max_seconds: float | None = None) -> None:
        self._max = max_seconds

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        seconds = trace.duration_seconds
        ok = self._max is None or seconds <= self._max
        return Score(
            value=1.0 if ok else 0.0,
            passed=ok,
            reason=f"{seconds:.2f}s",
            measurement=seconds,
        )
