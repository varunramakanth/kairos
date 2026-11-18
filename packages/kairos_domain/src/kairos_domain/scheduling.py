"""Data the scheduler and its store exchange (see ``kairos_scheduler.protocols``).

They live in the domain package so ``kairos_db`` can implement the store without
importing the scheduler.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kairos_domain.content import Message
from kairos_domain.jobs import Job, JobRun, JobRunStatus, JobStatus, Observation
from kairos_domain.run import RunRequest, StopReason


@dataclass(frozen=True)
class PreviousAlert:
    dedupe_key: str
    at: datetime


@dataclass(frozen=True)
class Claim:
    """A due job, leased to this scheduler until the lease expires."""

    job: Job
    lease_id: str


@dataclass(frozen=True)
class Firing:
    """Everything one firing writes: the job run, the agent run, the next schedule."""

    job_run_id: str
    request: RunRequest
    scheduled_for: datetime
    next_run_at: datetime | None
    status: JobStatus


@dataclass(frozen=True)
class Finished:
    """A job run whose agent run has stopped, with the context needed to judge it."""

    job: Job
    job_run: JobRun
    stop_reason: StopReason | None
    error: str | None
    messages: Sequence[Message]
    previous: Observation | None
    last_alert: PreviousAlert | None


@dataclass(frozen=True)
class Settlement:
    """What to record about a finished job run."""

    status: JobRunStatus
    error: str | None
    observation: Observation | None
    matched: bool
    alert: str | None
    dedupe_key: str | None
    consecutive_failures: int
    next_run_at: datetime | None
    """Replacement for the job's next run (backoff), or ``None`` to keep it."""
