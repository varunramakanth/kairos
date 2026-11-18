"""Test doubles for the harness seams."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from pydantic import JsonValue

from kairos_domain.content import ContentBlock, Message, TextBlock, ToolUseBlock
from kairos_domain.model import (
    ModelCompleted,
    ModelEvent,
    ModelRequest,
    ModelStopReason,
    ModelTextDelta,
    ModelToolCallStart,
)
from kairos_domain.run import ToolCallRecord
from kairos_domain.tools import ToolContext, ToolOutput, ToolSpec
from kairos_domain.usage import Usage

type Turn = Sequence[ModelEvent] | Exception

DEFAULT_USAGE = Usage(input_tokens=100, output_tokens=20)


def text_turn(
    text: str, *, usage: Usage = DEFAULT_USAGE, model: str = "scripted"
) -> list[ModelEvent]:
    """A model turn that answers with text and ends."""
    return [
        ModelTextDelta(text=text),
        ModelCompleted(
            message=Message(role="assistant", content=(TextBlock(text=text),)),
            stop_reason=ModelStopReason.END_TURN,
            usage=usage,
            model=model,
        ),
    ]


def tool_turn(
    *calls: tuple[str, str, dict[str, JsonValue]],
    text: str = "",
    usage: Usage = DEFAULT_USAGE,
    model: str = "scripted",
) -> list[ModelEvent]:
    """A model turn that requests tools. Each call is ``(id, name, input)``."""
    blocks: list[ContentBlock] = [TextBlock(text=text)] if text else []
    events: list[ModelEvent] = [ModelTextDelta(text=text)] if text else []
    for call_id, name, tool_input in calls:
        blocks.append(ToolUseBlock(id=call_id, name=name, input=tool_input))
        events.append(ModelToolCallStart(tool_call_id=call_id, name=name))
    events.append(
        ModelCompleted(
            message=Message(role="assistant", content=tuple(blocks)),
            stop_reason=ModelStopReason.TOOL_USE,
            usage=usage,
            model=model,
        )
    )
    return events


class ScriptedProvider:
    """Plays back a fixed list of turns, one per model call.

    A turn that is an exception is raised instead. ``requests`` records what the
    loop asked for, so tests can assert on the exact history the model saw.
    """

    def __init__(self, turns: Sequence[Turn], *, name: str = "scripted") -> None:
        self._turns = list(turns)
        self._name = name
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self._turns:
            raise AssertionError("ScriptedProvider ran out of turns")
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for event in turn:
            yield event


type ToolFn = Callable[[ToolCallRecord, ToolContext], Awaitable[str]]


class StaticToolExecutor:
    """A minimal ``ToolExecutor`` built from ``(spec, function)`` pairs."""

    def __init__(self, tools: Sequence[tuple[ToolSpec, ToolFn]] = ()) -> None:
        self._tools = {spec.name: (spec, fn) for spec, fn in tools}
        self.calls: list[ToolCallRecord] = []

    def specs(self) -> Sequence[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

    async def execute(self, call: ToolCallRecord, context: ToolContext) -> ToolOutput:
        self.calls.append(call)
        _, fn = self._tools[call.name]
        return ToolOutput(content=await fn(call, context))


def simple_spec(name: str, **overrides: object) -> ToolSpec:
    """A ``ToolSpec`` with an open object schema, for tests."""
    return ToolSpec.model_validate(
        {
            "name": name,
            "description": f"Test tool {name}",
            "input_schema": {"type": "object"},
            **overrides,
        }
    )
