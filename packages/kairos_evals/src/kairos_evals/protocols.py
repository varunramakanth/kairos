"""The three extension points of the framework (PLAN.md section 4.4)."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from pydantic import BaseModel

from kairos_evals.trace import Trace
from kairos_evals.types import EvalResult, Example, Kind, Layer, Score


class Metric(Protocol):
    """Scores one trace, optionally against a golden example.

    ``layer`` places the metric in the three-layer library: ``outcome`` (the final
    response), ``component`` (tool use, memory, planning, ...) or ``model``.
    ``kind`` says how it is computed: code, an LLM judge, or a human.
    """

    id: str
    layer: Layer
    kind: Kind

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score: ...


class TraceFilter(BaseModel):
    """Restricts which traces a source yields."""

    trace_ids: frozenset[str] | None = None
    attribute_equals: dict[str, str] = {}
    """Keep traces whose root span has all of these attribute values."""


class TraceSource(Protocol):
    """Where traces come from: OTLP/JSON files today; Langfuse, S3 or AgentCore later."""

    def load(self, filter: TraceFilter | None = None) -> AsyncIterator[Trace]: ...


class ResultSink(Protocol):
    """Where results go: stdout and JSONL today; S3 (Parquet) and Postgres later."""

    async def write(self, results: Sequence[EvalResult]) -> None: ...
