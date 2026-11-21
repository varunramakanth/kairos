"""Scheduled jobs and watches.

A job fires on a schedule and each firing is an ordinary agent run. A *watch* is a
job with structure: the run records one observation of a target, and plain code
(not the model) decides whether the observation is worth an alert.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from kairos_domain.base import DomainModel
from kairos_domain.users import LOCAL_USER

DEFAULT_TIMEZONE = "America/Toronto"


# --- schedules ------------------------------------------------------------------------


class CronSchedule(DomainModel):
    """Standard five-field cron, evaluated in local wall-clock time of ``timezone``."""

    kind: Literal["cron"] = "cron"
    expression: str = Field(description="Five-field cron, e.g. '0 8 * * mon-fri'.")
    timezone: str = DEFAULT_TIMEZONE


class IntervalSchedule(DomainModel):
    """Every N seconds of elapsed time (daylight saving does not affect it)."""

    kind: Literal["interval"] = "interval"
    every_seconds: int = Field(ge=300, description="At least 300 (five minutes).")


class OneShotSchedule(DomainModel):
    """Once, at an exact instant."""

    kind: Literal["one_shot"] = "one_shot"
    at: AwareDatetime


type Schedule = Annotated[
    CronSchedule | IntervalSchedule | OneShotSchedule, Field(discriminator="kind")
]


# --- watches ----------------------------------------------------------------------------


class Below(DomainModel):
    kind: Literal["below"] = "below"
    threshold: float


class Above(DomainModel):
    kind: Literal["above"] = "above"
    threshold: float


class Contains(DomainModel):
    """The observed text contains ``text`` (case-insensitive)."""

    kind: Literal["contains"] = "contains"
    text: str = Field(min_length=1)


class Changed(DomainModel):
    """The value moved by at least ``min_delta`` since the previous observation."""

    kind: Literal["changed"] = "changed"
    min_delta: float = Field(default=0.0, ge=0)


type Condition = Annotated[Below | Above | Contains | Changed, Field(discriminator="kind")]


class WatchSpec(DomainModel):
    """What to look at, what to extract, and when to raise an alert."""

    target: str = Field(
        min_length=1,
        max_length=2000,
        description="A URL, or a search query, describing where to look.",
    )
    extract: str = Field(
        min_length=1,
        max_length=500,
        description="What to read from the target, e.g. 'lowest round-trip fare in CAD'.",
    )
    condition: Condition
    cooldown_seconds: int = Field(
        default=6 * 3600, ge=0, description="Minimum time between two alerts."
    )


# --- jobs -------------------------------------------------------------------------------


class JobKind(StrEnum):
    TASK = "task"
    """Run a prompt on a schedule."""
    WATCH = "watch"
    """Observe a target on a schedule and alert on a condition."""
    DIGEST = "digest"
    """Send the weekly digest. Built from data; no agent run."""


class JobStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    """Proposed by the agent; does nothing until the user confirms it."""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    """A one-shot job that has fired."""
    EXPIRED = "expired"
    DELETED = "deleted"


class JobSpec(DomainModel):
    """The editable part of a job."""

    name: str = Field(min_length=1, max_length=120)
    kind: JobKind
    schedule: Schedule
    prompt: str | None = Field(
        default=None, max_length=20_000, description="For tasks: what the agent should do."
    )
    watch: WatchSpec | None = None
    expires_at: AwareDatetime | None = None
    model: str | None = None


class Job(DomainModel):
    id: str
    user_id: str = LOCAL_USER
    spec: JobSpec
    status: JobStatus
    next_run_at: datetime | None
    last_run_at: datetime | None
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime


class JobRunStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobRun(DomainModel):
    """One firing of a job and the agent run it started."""

    id: str
    job_id: str
    run_id: str | None
    """``None`` for firings without an agent run (the digest)."""
    scheduled_for: datetime
    status: JobRunStatus
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class Observation(DomainModel):
    """What a watch run saw. ``value`` is set when the extract is a number."""

    found: bool
    value: float | None = None
    unit: str | None = None
    text: str = Field(default="", max_length=2000)
    source_url: str | None = None


class StoredObservation(DomainModel):
    id: int
    job_id: str
    run_id: str
    observation: Observation
    matched: bool
    created_at: datetime


class Alert(DomainModel):
    id: int
    job_id: str
    run_id: str
    message: str
    dedupe_key: str
    created_at: datetime


class JobDetail(DomainModel):
    job: Job
    runs: list[JobRun]
    observations: list[StoredObservation]
    alerts: list[Alert]


class JobSummary(DomainModel):
    """A row of the Tasks page."""

    job: Job
    last_run: JobRun | None
    recent_values: list[float]
    """Up to the last 30 numeric observations, oldest first (the sparkline)."""
    alert_count: int
