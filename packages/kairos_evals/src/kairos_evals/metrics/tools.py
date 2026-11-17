"""Layer 2 (components): tool use."""

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score


class ToolSelectionAccuracy:
    """Did the agent call the tools a correct run calls, and no others?

    The score is the Jaccard overlap between the set of tools called and the set
    expected, so one wrong or missing tool costs partial credit. Order and repeat
    calls are not judged here (redundant calls get their own metric later).
    """

    id = "tool_selection_accuracy"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or example.expected_tools is None:
            return Score(value=1.0, passed=True, reason="no expected tools to compare against")
        called, expected = set(trace.tool_names), set(example.expected_tools)
        union = called | expected
        value = len(called & expected) / len(union) if union else 1.0
        problems: list[str] = []
        if missing := sorted(expected - called):
            problems.append(f"missing {missing}")
        if extra := sorted(called - expected):
            problems.append(f"unexpected {extra}")
        return Score(value=value, passed=value == 1.0, reason="; ".join(problems) or "exact match")
