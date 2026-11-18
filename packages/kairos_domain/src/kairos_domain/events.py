"""Run events: the typed stream a run emits (ADR-002).

Names follow AG-UI where it has an equivalent. Durable events carry a ``seq`` that is
monotonic within the run and are stored in ``run_events``; a client resumes a stream
by sending the last ``seq`` it saw. Ephemeral events (``seq`` is ``None``) are
live-only: starts and deltas are ephemeral, and the durable ``*_end`` event carries
the complete value, so a model stream that fails halfway leaves nothing to undo.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from kairos_domain.base import DomainModel
from kairos_domain.content import Provenance
from kairos_domain.model import ModelStopReason
from kairos_domain.run import StopReason
from kairos_domain.tools import ToolRisk
from kairos_domain.usage import BudgetSpent, Usage


class _Base(DomainModel):
    run_id: str
    ts: datetime


class DurableEvent(_Base):
    """A durable event: stored, replayable, addressed by ``seq``."""

    seq: int


class EphemeralEvent(_Base):
    """A live-only event. Never stored; safe to miss."""

    seq: None = None


class RunStarted(DurableEvent):
    type: Literal["run_started"] = "run_started"
    model: str
    input: str


class RunResumed(DurableEvent):
    """A process picked up an existing run from its checkpoint."""

    type: Literal["run_resumed"] = "run_resumed"
    attempt: int
    from_step: int


class StepStarted(DurableEvent):
    type: Literal["step_started"] = "step_started"
    step: int


class ModelCallFinished(DurableEvent):
    """One model call completed. ``model`` is the model that actually answered."""

    type: Literal["model_call_finished"] = "model_call_finished"
    step: int
    model: str
    stop_reason: ModelStopReason
    usage: Usage
    cost_usd: float


class ModelStreamReset(EphemeralEvent):
    """The model stream failed and is being retried. Discard deltas shown for this step."""

    type: Literal["model_stream_reset"] = "model_stream_reset"
    step: int
    reason: str


class StepFinished(DurableEvent):
    """The step's model call and its whole tool batch are done and checkpointed."""

    type: Literal["step_finished"] = "step_finished"
    step: int


class TextMessageStart(EphemeralEvent):
    type: Literal["text_message_start"] = "text_message_start"
    message_id: str


class TextMessageDelta(EphemeralEvent):
    type: Literal["text_message_delta"] = "text_message_delta"
    message_id: str
    delta: str


class TextMessageEnd(DurableEvent):
    type: Literal["text_message_end"] = "text_message_end"
    message_id: str
    text: str


class ToolCallStart(EphemeralEvent):
    type: Literal["tool_call_start"] = "tool_call_start"
    tool_call_id: str
    name: str


class ToolCallArgs(EphemeralEvent):
    type: Literal["tool_call_args"] = "tool_call_args"
    tool_call_id: str
    delta: str


class ToolCallEnd(DurableEvent):
    """The model finished writing the call. ``input`` is the complete, parsed input."""

    type: Literal["tool_call_end"] = "tool_call_end"
    tool_call_id: str
    name: str
    input: dict[str, JsonValue]


class ToolCallResult(DurableEvent):
    type: Literal["tool_call_result"] = "tool_call_result"
    tool_call_id: str
    name: str
    output: str
    """Raw tool output for display (truncated like the model's copy, but not wrapped)."""
    is_error: bool
    provenance: Provenance
    truncated: bool = False
    blob_id: str | None = None
    """Handle to the full output when ``truncated`` is true."""
    duration_ms: int
    suspected_injection: bool = False
    """Screening found instructions aimed at the agent in this (untrusted) output."""


class ToolCallUncertain(DurableEvent):
    """A non-idempotent call was interrupted; its outcome is unknown. The run pauses."""

    type: Literal["tool_call_uncertain"] = "tool_call_uncertain"
    tool_call_id: str
    name: str


class ApprovalRequested(DurableEvent):
    """A tool call waits for a human. The run pauses until every such call is decided."""

    type: Literal["approval_requested"] = "approval_requested"
    tool_call_id: str
    name: str
    reason: str
    input: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    risk: ToolRisk = ToolRisk.WRITE_SELF


class BudgetUpdated(DurableEvent):
    type: Literal["budget_updated"] = "budget_updated"
    spent: BudgetSpent


class ContextCompacted(DurableEvent):
    """The context grew too large and was replaced by a summary (a new epoch).

    The previous epoch's messages are kept in full as ``archive_blob_id``.
    """

    type: Literal["context_compacted"] = "context_compacted"
    epoch: int
    messages_summarized: int
    input_tokens_before: int
    summary: str
    archive_blob_id: str
    usage: Usage
    cost_usd: float


class CheckpointWritten(EphemeralEvent):
    type: Literal["checkpoint_written"] = "checkpoint_written"
    step: int
    through_seq: int
    """Every durable event up to and including this ``seq`` is stored."""


class RunFinished(DurableEvent):
    """The loop stopped. For pauses the run can continue later; otherwise it is final."""

    type: Literal["run_finished"] = "run_finished"
    stop_reason: StopReason
    output_text: str | None = None
    error: str | None = None
    usage: Usage
    cost_usd: float


type RunEvent = Annotated[
    RunStarted
    | RunResumed
    | StepStarted
    | ModelCallFinished
    | ModelStreamReset
    | StepFinished
    | TextMessageStart
    | TextMessageDelta
    | TextMessageEnd
    | ToolCallStart
    | ToolCallArgs
    | ToolCallEnd
    | ToolCallResult
    | ToolCallUncertain
    | ApprovalRequested
    | BudgetUpdated
    | ContextCompacted
    | CheckpointWritten
    | RunFinished,
    Field(discriminator="type"),
]
