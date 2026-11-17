"""Examples, scores and results."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

type Layer = Literal["outcome", "component", "model"]
type Kind = Literal["deterministic", "llm_judge", "human"]


class Example(BaseModel):
    """One golden-dataset case: an input and what a good run looks like."""

    model_config = ConfigDict(frozen=True)

    id: str
    input: str
    expected_tools: list[str] | None = None
    """Tools a correct run calls (order-insensitive). ``None`` means not asserted."""
    reference_answer: str | None = None
    budget: dict[str, float] = {}
    """Caps a correct run stays within. Keys: ``total_tokens``, ``cost_usd``,
    ``tool_calls``, ``model_calls``."""
    tags: list[str] = []
    metadata: dict[str, JsonValue] = {}


class Score(BaseModel):
    """A metric's verdict on one trace."""

    model_config = ConfigDict(frozen=True)

    value: float
    """Between 0 and 1; higher is better."""
    passed: bool
    reason: str = ""


class EvalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric_id: str
    layer: Layer
    kind: Kind
    trace_id: str
    example_id: str | None
    score: Score
