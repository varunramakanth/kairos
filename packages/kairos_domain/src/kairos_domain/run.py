"""A run: one durable execution of the agent loop."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from kairos_domain.base import DomainModel
from kairos_domain.content import Message
from kairos_domain.model import Effort
from kairos_domain.usage import Budget, BudgetSpent, Usage


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    """Waiting on a human (approval or input). ``stop_reason`` says which."""
    FINISHED = "finished"


class StopReason(StrEnum):
    """Why the loop stopped. Every exit from the loop names exactly one."""

    COMPLETED = "completed"
    BUDGET_EXCEEDED = "budget_exceeded"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    FAILED = "failed"

    @property
    def is_pause(self) -> bool:
        return self in (StopReason.AWAITING_APPROVAL, StopReason.AWAITING_INPUT)


class RunRequest(DomainModel):
    """Everything needed to start a run. Stored with the run so it can be resumed."""

    run_id: str
    input: str
    model: str
    system_prompt: str | None = None
    effort: Effort | None = None
    max_output_tokens: int = 32_000
    budget: Budget = Budget()
    thread_id: str | None = None
    history: tuple[Message, ...] = ()
    """Earlier turns of the thread, replayed unchanged before ``input``.

    Keeping them byte-identical (thinking blocks included) is what lets the
    provider's prompt cache hit across turns, and the API requires it for thinking.
    """
    metadata: dict[str, str] = Field(default_factory=dict[str, str])


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    """Marked and checkpointed before execution. Seen after a crash, it means the
    outcome is unknown."""
    UNCERTAIN = "uncertain"
    """A non-idempotent call was interrupted. A human decides what happens next."""


class ToolCallRecord(DomainModel):
    """A tool call the model requested that has no result in the history yet."""

    id: str
    name: str
    input: dict[str, JsonValue]
    status: ToolCallStatus = ToolCallStatus.PENDING

    @property
    def idempotency_key(self) -> str:
        """Stable across retries of the same call; tools pass it to external APIs."""
        return self.id


class RunState(BaseModel):
    """The checkpoint: enough to continue a run in a fresh process.

    Mutable on purpose. The loop owns one instance, updates it step by step and
    hands it to the ``Checkpointer``. Within an epoch ``messages`` only ever grows.
    Compaction starts a new epoch: the old messages are archived as a blob and
    replaced by a summary, so no earlier turn is ever edited in place.
    """

    model_config = ConfigDict(extra="forbid")

    request: RunRequest
    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason | None = None
    error: str | None = None
    messages: list[Message] = Field(default_factory=list[Message])
    epoch: int = 0
    """How many times this run's context has been compacted."""
    last_input_tokens: int = 0
    """Prompt size of the most recent model call (cached tokens included)."""
    pending_tool_calls: list[ToolCallRecord] = Field(default_factory=list[ToolCallRecord])
    step: int = 0
    usage: Usage = Usage()
    cost_usd: float = 0.0
    tool_calls_made: int = 0
    active_seconds: float = 0.0
    """Wall-clock time spent executing, summed across resumes. Pauses do not count."""
    next_seq: int = 0
    attempt: int = 0
    """How many times a process has picked this run up."""
    output_text: str | None = None

    @property
    def run_id(self) -> str:
        return self.request.run_id

    @property
    def spent(self) -> BudgetSpent:
        return BudgetSpent(
            total_tokens=self.usage.total_tokens,
            cost_usd=self.cost_usd,
            wall_seconds=self.active_seconds,
            tool_calls=self.tool_calls_made,
            steps=self.step,
        )
