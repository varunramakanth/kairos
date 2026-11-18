"""Layer 2 (components): planning. Are the steps of a visible plan carried through?"""

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score, not_applicable


class PlanCompletion:
    """When the agent writes a plan checklist, its last version has every step done.

    Reads the arguments of the last plan-tool call (``{"items": [{"step", "status"}]}``).
    The score is the share of steps marked completed. Runs that stop for approval
    are judged on the steps before the gated one, so they are not applicable here.
    """

    id = "plan_completion"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    def __init__(self, tool: str = "update_plan") -> None:
        self._tool = tool

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        calls = [r for r in trace.requested_tool_calls if r.name == self._tool]
        if not calls:
            return not_applicable("no plan")
        if trace.stop_reason not in (None, "completed"):
            return not_applicable(f"run stopped {trace.stop_reason}")
        items = calls[-1].arguments.get("items")
        steps = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        if not steps:
            return not_applicable("empty plan")
        done = [s for s in steps if s.get("status") == "completed"]
        open_steps = [str(s.get("step")) for s in steps if s.get("status") != "completed"]
        return Score(
            value=len(done) / len(steps),
            passed=not open_steps,
            reason=f"{len(done)}/{len(steps)} steps done"
            + (f"; open: {open_steps}" if open_steps else ""),
        )
