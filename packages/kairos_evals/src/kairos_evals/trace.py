"""The trace model every metric reads.

Deliberately small and framework-neutral: it is OpenTelemetry spans with the GenAI
semantic conventions, nothing Kairos-specific. Any agent that emits such spans can
be evaluated.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, JsonValue

OPERATION = "gen_ai.operation.name"
INPUT_ATTRIBUTE = "input.value"
OUTPUT_ATTRIBUTE = "output.value"
STOP_REASON = "kairos.stop_reason"
COST_USD = "kairos.cost_usd"
REQUESTED_TOOL_CALLS = "kairos.requested_tool_calls"
"""JSON list of ``{id, name, arguments}`` on a model-call span. Agents that do not emit
it are still evaluated; tool metrics then see executed calls only."""


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    start: datetime
    end: datetime
    attributes: dict[str, JsonValue] = {}
    is_error: bool = False

    @property
    def operation(self) -> str | None:
        value = self.attributes.get(OPERATION)
        return value if isinstance(value, str) else None

    def number(self, key: str) -> float:
        """A numeric attribute, or 0 when it is missing or not a number."""
        value = self.attributes.get(key)
        return (
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        )

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class Trace(BaseModel):
    """All spans of one trace, in start order."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    spans: tuple[Span, ...]

    @property
    def root(self) -> Span | None:
        ids = {span.span_id for span in self.spans}
        roots = [s for s in self.spans if s.parent_span_id is None or s.parent_span_id not in ids]
        return roots[0] if roots else None

    def with_operation(self, operation: str) -> Sequence[Span]:
        return [span for span in self.spans if span.operation == operation]

    @property
    def model_calls(self) -> Sequence[Span]:
        return self.with_operation("chat")

    @property
    def tool_calls(self) -> Sequence[Span]:
        return self.with_operation("execute_tool")

    @property
    def tool_names(self) -> list[str]:
        """Names of executed tools, in the order they started."""
        names = [span.attributes.get("gen_ai.tool.name") for span in self.tool_calls]
        return [name for name in names if isinstance(name, str)]

    @property
    def requested_tool_calls(self) -> list["ToolRequest"]:
        """Every tool call the model asked for, in order, including calls a policy then
        held for approval or denied. Needs content capture; falls back to the
        executed calls (without arguments) when the trace has none."""
        requests: list[ToolRequest] = []
        captured = False
        for span in self.model_calls:
            raw = span.attributes.get(REQUESTED_TOOL_CALLS)
            if not isinstance(raw, str):
                continue
            captured = True
            for item in json.loads(raw):
                requests.append(ToolRequest.model_validate(item))
        if captured:
            return requests
        return [
            ToolRequest(id=str(span.attributes.get("gen_ai.tool.call.id", "")), name=name)
            for span in self.tool_calls
            if isinstance(name := span.attributes.get("gen_ai.tool.name"), str)
        ]

    @property
    def requested_tool_names(self) -> list[str]:
        return [request.name for request in self.requested_tool_calls]

    def _root_text(self, key: str) -> str | None:
        root = self.root
        value = root.attributes.get(key) if root else None
        return value if isinstance(value, str) else None

    @property
    def input_text(self) -> str | None:
        return self._root_text(INPUT_ATTRIBUTE)

    @property
    def answer(self) -> str | None:
        """The final answer text (content capture only)."""
        return self._root_text(OUTPUT_ATTRIBUTE)

    @property
    def stop_reason(self) -> str | None:
        return self._root_text(STOP_REASON)

    @property
    def cost_usd(self) -> float:
        root = self.root
        if root is not None and COST_USD in root.attributes:
            return root.number(COST_USD)
        return sum(span.number("gen_ai.usage.cost") for span in self.model_calls)

    @property
    def duration_seconds(self) -> float:
        if not self.spans:
            return 0.0
        return (max(s.end for s in self.spans) - min(s.start for s in self.spans)).total_seconds()

    def tool_outputs(self) -> list[str]:
        """Text the tools returned (content capture only)."""
        return [
            value
            for span in self.tool_calls
            if isinstance(value := span.attributes.get(OUTPUT_ATTRIBUTE), str)
        ]


class ToolRequest(BaseModel):
    """A tool call as the model asked for it."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, JsonValue] = {}


def from_unix_nano(value: str | int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=UTC)
