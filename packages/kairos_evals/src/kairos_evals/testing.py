"""Test helpers: hand-built traces in the shape an instrumented agent emits."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from kairos_evals import Span, Trace

T0 = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)


def make_trace(
    *,
    trace_id: str = "t1",
    input: str = "do the thing",
    answer: str | None = "Done.",
    stop: str = "completed",
    requested: Sequence[tuple[str, dict[str, JsonValue]]] = (),
    executed: Sequence[tuple[str, str, bool, str]] = (),
    cost: float = 0.01,
    seconds: float = 2.0,
) -> Trace:
    """``requested`` is (name, arguments) per call the model asked for; ``executed`` is
    (name, risk, is_error, output) per call that actually ran."""
    root_attributes: dict[str, JsonValue] = {
        "gen_ai.operation.name": "invoke_agent",
        "input.value": input,
        "kairos.stop_reason": stop,
        "kairos.cost_usd": cost,
    }
    if answer is not None:
        root_attributes["output.value"] = answer
    spans = [
        Span(
            trace_id=trace_id,
            span_id="root",
            name="invoke_agent agent",
            start=T0,
            end=T0 + timedelta(seconds=seconds),
            attributes=root_attributes,
        ),
        Span(
            trace_id=trace_id,
            span_id="chat1",
            parent_span_id="root",
            name="chat model",
            start=T0 + timedelta(milliseconds=1),
            end=T0 + timedelta(milliseconds=500),
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.cost": cost,
                "kairos.requested_tool_calls": json.dumps(
                    [
                        {"id": f"c{i}", "name": n, "arguments": a}
                        for i, (n, a) in enumerate(requested)
                    ]
                ),
            },
        ),
    ]
    for i, (name, risk, is_error, output) in enumerate(executed):
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=f"tool{i}",
                parent_span_id="root",
                name=f"execute_tool {name}",
                start=T0 + timedelta(milliseconds=600 + i),
                end=T0 + timedelta(milliseconds=900 + i),
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": name,
                    "kairos.tool.risk": risk,
                    "kairos.tool.is_error": is_error,
                    "output.value": output,
                },
                is_error=is_error,
            )
        )
    return Trace(trace_id=trace_id, spans=tuple(sorted(spans, key=lambda s: s.start)))
