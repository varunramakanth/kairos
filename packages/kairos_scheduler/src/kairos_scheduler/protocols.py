"""The storage seam the scheduler and the schedule tools need.

``kairos_db.JobStore`` implements it on Postgres. The scheduler only decides; the
store makes each decision durable in one lease-guarded transaction.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from kairos_domain.governance import DigestData, Notification
from kairos_domain.jobs import Job, JobSpec, JobStatus
from kairos_domain.scheduling import Claim, Finished, Firing, Settlement

__all__ = ["Claim", "Finished", "Firing", "JobRepository", "SchedulerStore", "Settlement"]


class JobRepository(Protocol):
    """Create, read and change one user's jobs (ADR-018). Used by the API and the
    schedule tools."""

    async def create(
        self,
        job_id: str,
        spec: JobSpec,
        *,
        owner: str,
        status: JobStatus,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job:
        """Insert a job; creating an existing id returns the existing job unchanged."""
        ...

    async def get(self, job_id: str, *, owner: str) -> Job | None:
        """``owner``'s job, or ``None`` (another user's job looks missing)."""
        ...

    async def list_jobs(self, *, owner: str, include_deleted: bool = False) -> Sequence[Job]: ...

    async def set_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        owner: str,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job | None: ...


class SchedulerStore(JobRepository, Protocol):
    """The scheduler loop's view: claim, fire, close and settle."""

    async def claim_due(self, *, now: datetime, ttl_seconds: float) -> Claim | None:
        """Lease the most overdue active job with no run in progress, if any."""
        ...

    async def fire(self, claim: Claim, firing: Firing, *, now: datetime) -> bool:
        """Queue the run and advance the job in one transaction. ``False`` if the
        lease was lost (another scheduler owns the job)."""
        ...

    async def fire_digest(
        self,
        claim: Claim,
        *,
        job_run_id: str,
        scheduled_for: datetime,
        next_run_at: datetime | None,
        notification: Notification,
        now: datetime,
    ) -> bool:
        """Record a digest firing and queue its notification."""
        ...

    async def digest_data(self, *, owner: str, since: datetime, until: datetime) -> DigestData:
        """One user's week, for their digest."""
        ...

    async def close(self, claim: Claim, status: JobStatus, *, now: datetime) -> bool:
        """End a job without firing it (expired)."""
        ...

    async def settle_next(self, decide: Callable[[Finished], Settlement], *, now: datetime) -> bool:
        """Settle one finished job run with ``decide``. ``False`` when none is waiting."""
        ...
