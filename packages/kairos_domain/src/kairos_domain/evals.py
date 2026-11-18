"""Eval results as the API and dashboard see them.

These mirror what ``kairos_evals`` writes to Postgres. They are defined here, not
imported from ``kairos_evals``, so the API's schema never depends on the eval
library (which in turn never imports Kairos).
"""

from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from kairos_domain.base import DomainModel

type EvalLayer = Literal["outcome", "component", "model"]
type EvalKind = Literal["deterministic", "llm_judge", "human"]


class EvalMetricSummary(DomainModel):
    metric_id: str
    layer: EvalLayer
    kind: EvalKind
    n: int
    mean: float
    pass_rate: float
    p50: float | None = None
    p95: float | None = None


class EvalRunSummary(DomainModel):
    id: str
    suite: str
    harness: str
    harness_version: str
    model: str
    git_sha: str | None
    started_at: datetime
    finished_at: datetime
    examples: int
    passed: bool | None
    summary: list[EvalMetricSummary]
    gate: dict[str, JsonValue]


class EvalResultRow(DomainModel):
    example_id: str | None
    trace_id: str
    metric_id: str
    layer: EvalLayer
    kind: EvalKind
    value: float
    passed: bool
    applicable: bool
    measurement: float | None
    reason: str
    details: dict[str, JsonValue]


class EvalRunDetail(DomainModel):
    run: EvalRunSummary
    results: list[EvalResultRow]


class ReviewItem(DomainModel):
    """One judge verdict for a human to label. The judge's own verdict is included so
    the UI can reveal it after the human has answered."""

    run_id: str
    suite: str
    example_id: str
    metric_id: str
    question: str
    answer: str
    criterion: str
    """What the judge was asked about this answer."""
    judge_passed: bool
    judge_score: int | None
    judge_reason: str


class ReviewLabel(DomainModel):
    run_id: str
    example_id: str
    metric_id: str
    passed: bool
    note: str | None = None


class CriterionCalibration(DomainModel):
    """How well the judge agrees with the owner on one criterion."""

    metric_id: str
    n: int
    agreement: float
    kappa: float | None
    """Cohen's kappa; ``None`` until there are labels on both outcomes."""
    trusted: bool
    """At least ``MIN_LABELS`` labels and kappa of at least ``MIN_KAPPA``."""
