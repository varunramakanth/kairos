"""Examples, scores, results and eval runs."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

type Layer = Literal["outcome", "component", "model"]
type Kind = Literal["deterministic", "llm_judge", "human"]
type Intent = Literal["chat", "research", "schedule", "watch", "action", "clarify"]
"""What the user wanted, as the evaluator infers it from the tools the agent asked for:

- ``chat``: answered directly, no tools that change anything or reach the web
- ``research``: searched or read the web
- ``schedule``: proposed a recurring or one-time task
- ``watch``: proposed a watch on a value with an alert condition
- ``action``: asked to do something with a side effect (send a message, change a job)
- ``clarify``: asked the user a question instead of acting
"""


class ExpectedSchedule(BaseModel):
    """What a correct ``schedule_create`` proposal contains.

    ``schedule`` uses the agent's own schedule shape (``{"kind": "cron", "expression":
    ..., "timezone": ...}``, ``{"kind": "interval", "every_seconds": ...}`` or
    ``{"kind": "one_shot", "at": ...}``). Cron schedules are compared by their next
    firings, not by text, so ``0 9 * * 1-5`` and ``0 9 * * mon-fri`` are equal.
    """

    model_config = ConfigDict(frozen=True)

    job_kind: Literal["task", "watch"]
    schedule: dict[str, JsonValue] | None = None
    """``None`` when the request leaves the timing to the agent (any schedule passes)."""
    also_accept: list[dict[str, JsonValue]] = []
    """Other schedules that are equally correct readings of the request."""
    condition: dict[str, JsonValue] | None = None
    """For watches: ``{"kind": "below", "threshold": 1500}`` and so on."""
    expires_at: AwareDatetime | None = None
    """When the job should stop; the proposal must agree within a day."""


class Simulation(BaseModel):
    """A multi-turn case driven by an LLM user simulator instead of a fixed input."""

    model_config = ConfigDict(frozen=True)

    persona: str
    """Who the simulated user is and how they talk."""
    goal: str
    """What they want; the simulator ends the conversation once it is met."""
    success: str | None = None
    """What a good outcome is, when that differs from the user's goal. Needed for
    adversarial personas: their goal is an attack a good assistant refuses."""
    max_turns: int = Field(default=4, ge=1, le=10)


class Example(BaseModel):
    """One golden-dataset case: an input and what a good run looks like.

    Every expectation is optional. A metric with nothing to compare against marks
    its score not applicable, and it is left out of the summary.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    input: str
    intent: Intent | None = None
    expected_tools: list[str] | None = None
    """Tools a correct run asks for (order-insensitive). ``None`` means not asserted."""
    allowed_tools: list[str] = []
    """Tools a correct run may use or skip without penalty (a fetch after a search)."""
    forbidden_tools: list[str] = []
    """Tools a correct run must never ask for (for example a send after an injection)."""
    expected_tool_args: dict[str, dict[str, JsonValue]] = {}
    """Per tool, a partial argument object one of its calls must match. Strings match
    case-insensitively as substrings; other values must be equal; objects recurse."""
    expected_schedule: ExpectedSchedule | None = None
    expected_stop_reason: str | None = None
    """For example ``completed``, or ``awaiting_approval`` for a gated action."""
    reference_answer: str | None = None
    rubric: str | None = None
    """What a judge should look for, in plain words."""
    canary: str | None = None
    """A string planted in untrusted content that must never reach the answer or a tool."""
    simulation: Simulation | None = None
    now: AwareDatetime | None = None
    """The agent's clock for this case, so relative times are reproducible."""
    budget: dict[str, float] = {}
    """Caps a correct run stays within. Keys: ``total_tokens``, ``cost_usd``,
    ``tool_calls``, ``model_calls``."""
    tags: list[str] = []
    metadata: dict[str, JsonValue] = {}


class Score(BaseModel):
    """A metric's verdict on one trace."""

    model_config = ConfigDict(frozen=True)

    value: float = Field(ge=0.0, le=1.0)
    """Between 0 and 1; higher is better."""
    passed: bool
    reason: str = ""
    applicable: bool = True
    """``False`` when the example gives the metric nothing to check. Not applicable
    scores are excluded from means and pass rates."""
    measurement: float | None = None
    """A raw quantity behind the score (seconds, dollars), summarized as p50/p95."""
    details: dict[str, JsonValue] = {}


def not_applicable(reason: str) -> Score:
    return Score(value=1.0, passed=True, reason=reason, applicable=False)


class EvalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric_id: str
    layer: Layer
    kind: Kind
    trace_id: str
    example_id: str | None
    score: Score


class MetricSummary(BaseModel):
    """One metric over a suite: mean, pass rate and, for measurements, percentiles."""

    model_config = ConfigDict(frozen=True)

    metric_id: str
    layer: Layer
    kind: Kind
    n: int
    """Applicable scores only."""
    mean: float
    pass_rate: float
    p50: float | None = None
    p95: float | None = None


class EvalRun(BaseModel):
    """One evaluation of one suite against one harness and model."""

    model_config = ConfigDict(frozen=True)

    id: str
    suite: str
    harness: str
    harness_version: str
    model: str
    started_at: datetime
    finished_at: datetime
    git_sha: str | None = None
    examples: int
    summary: list[MetricSummary]
    passed: bool | None = None
    """Set by the regression gate; ``None`` when the run was not gated."""
    gate: dict[str, JsonValue] = {}
