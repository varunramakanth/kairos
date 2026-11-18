"""The provider-neutral model call: one request in, a stream of events out."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from kairos_domain.base import DomainModel
from kairos_domain.content import Message
from kairos_domain.tools import ToolSpec
from kairos_domain.usage import Usage

type Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelRequest(DomainModel):
    model: str
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 32_000
    effort: Effort | None = None


class ModelStopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


class ModelTextDelta(DomainModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ModelToolCallStart(DomainModel):
    type: Literal["tool_call_start"] = "tool_call_start"
    tool_call_id: str
    name: str


class ModelToolCallArgsDelta(DomainModel):
    type: Literal["tool_call_args_delta"] = "tool_call_args_delta"
    tool_call_id: str
    partial_json: str


class ModelCompleted(DomainModel):
    """Always the last event of a successful stream. Carries the full assistant message."""

    type: Literal["completed"] = "completed"
    message: Message
    stop_reason: ModelStopReason
    usage: Usage
    model: str
    """The model that actually answered (may differ from the request after a fallback)."""
    refusal_category: str | None = None


type ModelEvent = Annotated[
    ModelTextDelta | ModelToolCallStart | ModelToolCallArgsDelta | ModelCompleted,
    Field(discriminator="type"),
]
