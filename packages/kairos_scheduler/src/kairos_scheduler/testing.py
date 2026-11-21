"""Test helpers for code that uses the scheduler's storage seam."""

from collections.abc import Sequence
from datetime import datetime

from kairos_domain.jobs import Job, JobSpec, JobStatus


class MemoryJobs:
    """A ``JobRepository`` held in a dict, for tests and offline evals."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

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
        if job_id not in self.jobs:
            self.jobs[job_id] = Job(
                id=job_id,
                user_id=owner,
                spec=spec,
                status=status,
                next_run_at=next_run_at,
                last_run_at=None,
                consecutive_failures=0,
                created_at=now,
                updated_at=now,
            )
        job = self.jobs[job_id]
        if job.user_id != owner:
            raise ValueError(f"job {job_id} belongs to another user")
        return job

    async def get(self, job_id: str, *, owner: str) -> Job | None:
        job = self.jobs.get(job_id)
        return job if job is not None and job.user_id == owner else None

    async def list_jobs(self, *, owner: str, include_deleted: bool = False) -> Sequence[Job]:
        return [job for job in self.jobs.values() if job.user_id == owner]

    async def set_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        owner: str,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job | None:
        current = await self.get(job_id, owner=owner)
        if current is None:
            return None
        job = current.model_copy(update={"status": status, "next_run_at": next_run_at})
        self.jobs[job_id] = job
        return job
