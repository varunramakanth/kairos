"""Messages and content blocks: the conversation the model sees."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from kairos_domain.base import DomainModel


class Provenance(StrEnum):
    """Where content came from, which decides whether it may be treated as instructions.

    ``UNTRUSTED`` content (web pages, email, documents, third-party APIs) is data.
    The harness wraps it in delimited blocks before the model sees it.
    """

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class TextBlock(DomainModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(DomainModel):
    """The model asking for a tool to be called."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, JsonValue]


class ToolResultBlock(DomainModel):
    """The outcome of a tool call, exactly as the model saw it.

    ``content`` is already truncated and, for untrusted tools, already wrapped.
    """

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False
    provenance: Provenance = Provenance.TRUSTED


class OpaqueBlock(DomainModel):
    """A provider-native block the harness does not interpret but must replay verbatim.

    Thinking blocks are the main case: current Claude models reject a history in
    which they were altered or dropped selectively. ``data`` is the provider's own
    JSON for the block; ``provider`` says which provider can read it back.
    """

    type: Literal["opaque"] = "opaque"
    provider: str
    kind: str
    data: dict[str, JsonValue]


type ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | OpaqueBlock, Field(discriminator="type")
]


class Message(DomainModel):
    role: Literal["user", "assistant"]
    content: tuple[ContentBlock, ...]

    @property
    def text(self) -> str:
        """All text blocks joined; empty if there are none."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.content if isinstance(b, ToolUseBlock))


def user_text(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))
