"""Kairos-specific policy metrics: budget-cap adherence."""

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score


class BudgetAdherence:
    """Did the run stay within the caps set for it?

    Usage is computed from the trace itself: tokens and cost summed over model-call
    spans, tool calls and model calls counted from spans. ``cost_attribute`` is the
    per-call cost attribute (Langfuse's ``gen_ai.usage.cost`` by default).
    """

    id = "budget_adherence"
    layer: Layer = "outcome"
    kind: Kind = "deterministic"

    def __init__(self, cost_attribute: str = "gen_ai.usage.cost") -> None:
        self._cost_attribute = cost_attribute

    def usage(self, trace: Trace) -> dict[str, float]:
        calls = trace.model_calls
        return {
            "total_tokens": sum(
                c.number("gen_ai.usage.input_tokens") + c.number("gen_ai.usage.output_tokens")
                for c in calls
            ),
            "cost_usd": sum(c.number(self._cost_attribute) for c in calls),
            "tool_calls": float(len(trace.tool_calls)),
            "model_calls": float(len(calls)),
        }

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or not example.budget:
            return Score(value=1.0, passed=True, reason="no budget to check")
        usage = self.usage(trace)
        unknown = sorted(set(example.budget) - set(usage))
        if unknown:
            raise ValueError(f"example {example.id}: unknown budget keys {unknown}")
        over = [
            f"{key} {usage[key]:g} > {cap:g}"
            for key, cap in example.budget.items()
            if usage[key] > cap
        ]
        return Score(
            value=0.0 if over else 1.0, passed=not over, reason="; ".join(over) or "within budget"
        )
