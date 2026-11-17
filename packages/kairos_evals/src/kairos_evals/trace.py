"""The trace model every metric reads.

Deliberately small and framework-neutral: it is OpenTelemetry spans with the GenAI
semantic conventions, nothing Kairos-specific. Any agent that emits such spans can
be evaluated.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, JsonValue

OPERATION = "gen_ai.operation.name"


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


def from_unix_nano(value: str | int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=UTC)
