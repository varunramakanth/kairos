"""Layer 2 (components): tool use."""

import json

from pydantic import JsonValue

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score, not_applicable

HELPER_TOOLS = frozenset({"update_plan", "current_time"})
"""Tools that support a run without being the answer to it. Selection ignores them
unless an example expects one explicitly."""


class ToolSelectionAccuracy:
    """Did the agent ask for the tools a correct run asks for, and no others?

    The score is the Jaccard overlap between the set of tools requested and the set
    expected, so one wrong or missing tool costs partial credit. Requested (not just
    executed) calls count, so a send held for approval still shows as chosen. Helper
    tools are ignored unless expected. Order and repeats are judged elsewhere.
    """

    id = "tool_selection_accuracy"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    def __init__(self, helper_tools: frozenset[str] = HELPER_TOOLS) -> None:
        self._helpers = helper_tools

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or example.expected_tools is None:
            return not_applicable("no expected tools to compare against")
        expected = set(example.expected_tools)
        called = {n for n in trace.requested_tool_names if n in expected or n not in self._helpers}
        union = called | expected
        value = len(called & expected) / len(union) if union else 1.0
        problems: list[str] = []
        if missing := sorted(expected - called):
            problems.append(f"missing {missing}")
        if extra := sorted(called - expected):
            problems.append(f"unexpected {extra}")
        if forbidden := sorted(called & set(example.forbidden_tools)):
            problems.append(f"forbidden {forbidden}")
            value = 0.0
        return Score(value=value, passed=value == 1.0, reason="; ".join(problems) or "exact match")


def matches(expected: JsonValue, actual: JsonValue) -> bool:
    """Partial match: strings case-insensitively as substrings, objects recursively
    on the expected keys, lists element-wise in any order, the rest by equality."""
    if isinstance(expected, str):
        return isinstance(actual, str) and expected.strip().lower() in actual.lower()
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(matches(item, candidate) for candidate in actual) for item in expected
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(expected) - float(actual)) < 1e-9
    return expected == actual


class ToolParameterAccuracy:
    """Did the calls carry the right arguments?

    For each tool in ``expected_tool_args``, some call to it must match the partial
    argument object. The score is the share of tools matched.
    """

    id = "tool_parameter_accuracy"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or not example.expected_tool_args:
            return not_applicable("no expected arguments")
        requests = trace.requested_tool_calls
        failures: list[str] = []
        for name, expected in example.expected_tool_args.items():
            calls = [r for r in requests if r.name == name]
            if not calls:
                failures.append(f"{name} not called")
            elif not any(matches(expected, dict(r.arguments)) for r in calls):
                got = json.dumps(calls[-1].arguments, sort_keys=True)[:300]
                failures.append(f"{name} args {got} do not match {json.dumps(expected)}")
        total = len(example.expected_tool_args)
        value = (total - len(failures)) / total
        return Score(value=value, passed=not failures, reason="; ".join(failures) or "all match")


class ToolErrorRate:
    """Share of executed tool calls that did not fail. Failed calls are usually bad
    arguments the tool rejected, so this tracks how often the model misuses tools."""

    id = "tool_error_rate"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        calls = trace.tool_calls
        if not calls:
            return not_applicable("no tools executed")
        failed = [c for c in calls if c.is_error or c.attributes.get("kairos.tool.is_error")]
        names = sorted({str(c.attributes.get("gen_ai.tool.name")) for c in failed})
        return Score(
            value=1 - len(failed) / len(calls),
            passed=not failed,
            reason=f"{len(failed)}/{len(calls)} failed {names}" if failed else "no errors",
            measurement=len(failed) / len(calls),
        )


class RedundantCalls:
    """No identical call (same tool, same arguments) is requested twice in a run."""

    id = "redundant_call_rate"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        requests = trace.requested_tool_calls
        if not requests:
            return not_applicable("no tools requested")
        seen: set[str] = set()
        repeats: list[str] = []
        for request in requests:
            key = request.name + json.dumps(request.arguments, sort_keys=True)
            if key in seen:
                repeats.append(request.name)
            seen.add(key)
        return Score(
            value=1 - len(repeats) / len(requests),
            passed=not repeats,
            reason=f"repeated {repeats}" if repeats else "no repeats",
            measurement=len(repeats) / len(requests),
        )
