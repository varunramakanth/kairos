"""Layer 2 (components): intent detection and multi-turn clarification."""

from collections.abc import Collection

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Intent, Kind, Layer, Score, not_applicable

RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})
ACTION_TOOLS = frozenset({"notify_me", "schedule_set_status"})
SCHEDULE_TOOL = "schedule_create"


def infer_intent(
    trace: Trace,
    *,
    research_tools: Collection[str] = RESEARCH_TOOLS,
    action_tools: Collection[str] = ACTION_TOOLS,
    schedule_tool: str = SCHEDULE_TOOL,
) -> Intent:
    """What the agent took the request to be, read from the tools it asked for.

    Precedence runs from the most specific commitment to the least: a watch or task
    proposal, then a side-effecting action, then research. With no such tools, a
    final answer that ends in a question is a clarification; anything else is chat.
    Helper tools (clock, plan checklist) never decide the intent.
    """
    requests = trace.requested_tool_calls
    schedules = [r for r in requests if r.name == schedule_tool]
    if any(r.arguments.get("kind") == "watch" for r in schedules):
        return "watch"
    if schedules:
        return "schedule"
    names = {r.name for r in requests}
    if names & set(action_tools):
        return "action"
    if names & set(research_tools):
        return "research"
    answer = (trace.answer or "").strip()
    return "clarify" if answer.endswith("?") else "chat"


class IntentAccuracy:
    """Did the agent treat the request as the right kind of thing?

    The inferred intent and the expected one go into ``details`` so a report can
    build a confusion matrix across a suite.
    """

    id = "intent_accuracy"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or example.intent is None:
            return not_applicable("no expected intent")
        got = infer_intent(trace)
        ok = got == example.intent
        return Score(
            value=1.0 if ok else 0.0,
            passed=ok,
            reason=f"expected {example.intent}, got {got}",
            details={"expected": example.intent, "got": got},
        )


class ClarificationQuality:
    """Asks when the request is ambiguous, and does not ask when it is clear.

    Applies to every example with an expected intent: an unnecessary question is as
    much a failure as acting on a guess.
    """

    id = "clarification_quality"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or example.intent is None:
            return not_applicable("no expected intent")
        should_ask = example.intent == "clarify"
        asked = infer_intent(trace) == "clarify"
        if should_ask == asked:
            reason = "asked for clarification" if asked else "acted without asking"
            return Score(value=1.0, passed=True, reason=reason)
        reason = "acted on an ambiguous request" if should_ask else "asked when it was clear"
        return Score(value=0.0, passed=False, reason=reason)
