from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from kairos_domain.jobs import Job, JobSpec, JobStatus
from kairos_domain.tools import ToolContext
from kairos_harness import FakeClock
from kairos_scheduler.tools import job_id_for, schedule_tools

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)  # Tue 10:00 in Toronto


class MemoryJobs:
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


def ctx(call_id: str = "toolu_1") -> ToolContext:
    return ToolContext(run_id="run_1", tool_call_id=call_id, idempotency_key=call_id)


def tools(store: MemoryJobs) -> dict[str, Any]:
    return {t.spec.name: t for t in schedule_tools(store, FakeClock(NOW))}


WEEKDAY_BRIEF: dict[str, Any] = {
    "name": "Weekday brief",
    "kind": "task",
    "schedule": {"kind": "cron", "expression": "0 8 * * mon-fri", "timezone": "America/Toronto"},
    "prompt": "Summarize my day.",
}


async def test_create_proposes_a_pending_job_and_reads_back_local_times() -> None:
    store = MemoryJobs()
    output = await tools(store)["schedule_create"](WEEKDAY_BRIEF, ctx())

    assert not output.is_error
    (job,) = store.jobs.values()
    assert job.status is JobStatus.PENDING_CONFIRMATION
    assert job.id == job_id_for("toolu_1")
    assert job.next_run_at == datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    assert "Wed 18 Nov 2026, 08:00 EST" in output.content
    assert "Thu 19 Nov 2026, 08:00 EST" in output.content
    assert "NOT active yet" in output.content


async def test_create_is_idempotent_per_tool_call() -> None:
    store = MemoryJobs()
    create = tools(store)["schedule_create"]
    await create(WEEKDAY_BRIEF, ctx("toolu_1"))
    await create(WEEKDAY_BRIEF, ctx("toolu_1"))  # a retried call
    assert len(store.jobs) == 1


async def test_create_rejects_bad_schedules_with_a_reason_the_model_can_fix() -> None:
    store = MemoryJobs()
    create = tools(store)["schedule_create"]
    past = {**WEEKDAY_BRIEF, "schedule": {"kind": "one_shot", "at": "2026-11-17T08:00:00-05:00"}}
    output = await create(past, ctx())
    assert output.is_error
    assert "in the past" in output.content

    no_prompt = {**WEEKDAY_BRIEF, "prompt": None}
    assert "needs a prompt" in (await create(no_prompt, ctx("t2"))).content

    watch_without_spec = {**WEEKDAY_BRIEF, "kind": "watch"}
    assert "needs a watch spec" in (await create(watch_without_spec, ctx("t3"))).content
    assert store.jobs == {}


async def test_the_agent_cannot_activate_an_unconfirmed_job() -> None:
    store = MemoryJobs()
    named = tools(store)
    await named["schedule_create"](WEEKDAY_BRIEF, ctx())
    job_id = job_id_for("toolu_1")

    output = await named["schedule_set_status"]({"job_id": job_id, "status": "active"}, ctx("t2"))
    assert output.is_error
    assert store.jobs[job_id].status is JobStatus.PENDING_CONFIRMATION

    paused = await named["schedule_set_status"]({"job_id": job_id, "status": "paused"}, ctx("t3"))
    assert "is now paused" in paused.content
    resumed = await named["schedule_set_status"]({"job_id": job_id, "status": "active"}, ctx("t4"))
    assert "is now active" in resumed.content
    assert store.jobs[job_id].next_run_at is not None


async def test_list_summarizes_jobs() -> None:
    store = MemoryJobs()
    named = tools(store)
    assert (await named["schedule_list"]({}, ctx())).content == "No scheduled jobs."
    await named["schedule_create"](WEEKDAY_BRIEF, ctx())
    listing = (await named["schedule_list"]({}, ctx("t2"))).content
    assert "Weekday brief [task, pending_confirmation] cron '0 8 * * mon-fri'" in listing
