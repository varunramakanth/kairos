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
        status: JobStatus,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job:
        if job_id not in self.jobs:
            self.jobs[job_id] = Job(
                id=job_id,
                spec=spec,
                status=status,
                next_run_at=next_run_at,
                last_run_at=None,
                consecutive_failures=0,
                created_at=now,
                updated_at=now,
            )
        return self.jobs[job_id]

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def list_jobs(self, *, include_deleted: bool = False) -> Sequence[Job]:
        return list(self.jobs.values())

    async def set_status(
        self, job_id: str, status: JobStatus, *, next_run_at: datetime | None, now: datetime
    ) -> Job | None:
        job = self.jobs[job_id].model_copy(update={"status": status, "next_run_at": next_run_at})
        self.jobs[job_id] = job
        return job
