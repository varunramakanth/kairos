"""Agent tools for scheduling: propose, list and pause jobs, and record observations.

A job the agent proposes starts as ``pending_confirmation`` and does nothing until
the user confirms it in the UI. The tool's result repeats the schedule back in
local time so the model can confirm it in words too. The agent may pause, resume
or delete jobs, but it can never activate a job the user has not confirmed.
"""

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from kairos_domain.jobs import JobKind, JobSpec, JobStatus, Observation
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_harness import Clock
from kairos_scheduler.protocols import JobRepository
from kairos_scheduler.scheduler import summarize
from kairos_scheduler.timing import ScheduleError, describe, local_text, upcoming, validate
from kairos_scheduler.watch import OBSERVATION_TOOL
from kairos_tools import Tool, tool

CREATE_TOOL = "schedule_create"


def job_id_for(idempotency_key: str) -> str:
    """Derived from the tool call, so a retried call cannot create a second job."""
    return "job_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]


def record_observation_tool() -> Tool[Observation]:
    @tool(name=OBSERVATION_TOOL)
    async def record_observation(args: Observation, context: ToolContext) -> ToolOutput:
        """Record what a scheduled watch observed. Only use this when a scheduled
        watch run asks you to. Call it once, with found=false if the target or value
        could not be found."""
        return ToolOutput(content="Observation recorded.")

    return record_observation


class ListInput(BaseModel):
    pass


class SetStatusInput(BaseModel):
    job_id: str
    status: Literal["paused", "active", "deleted"] = Field(
        description="active resumes a paused job; it cannot activate an unconfirmed one."
    )


def schedule_tools(
    jobs: JobRepository, clock: Clock, *, timezone: str = "America/Toronto"
) -> list[Tool[Any]]:
    @tool(name=CREATE_TOOL, risk=ToolRisk.WRITE_SELF)
    async def schedule_create(args: JobSpec, context: ToolContext) -> ToolOutput:
        """Propose a scheduled task or watch for the user to confirm.

        Use kind=task with a prompt for recurring or one-time work ("every weekday at
        8am summarize ..."), and kind=watch with a watch spec to monitor a value and
        alert on a condition ("tell me when the fare drops under $1500"). Resolve
        relative times ("tomorrow 8am") with current_time first, and give one-shot
        times as ISO 8601 with an offset. The job stays inactive until the user
        confirms it in the UI; tell the user what you proposed and the next run times
        this tool returns."""
        now = clock.now()
        problems: list[str] = []
        if args.kind is JobKind.TASK and not args.prompt:
            problems.append("a task needs a prompt")
        if args.kind is JobKind.WATCH and args.watch is None:
            problems.append("a watch needs a watch spec")
        if args.kind is JobKind.DIGEST:
            problems.append("the digest is built in; use schedule_set_status to pause it")
        try:
            validate(args.schedule, now=now)
        except ScheduleError as error:
            problems.append(str(error))
        if problems:
            return ToolOutput(
                content="Cannot create the job: " + "; ".join(problems), is_error=True
            )
        fires = upcoming(args.schedule, after=now, count=3)
        job = await jobs.create(
            job_id_for(context.idempotency_key),
            args,
            owner=context.user_id,
            status=JobStatus.PENDING_CONFIRMATION,
            next_run_at=fires[0] if fires else None,
            now=now,
        )
        zone = getattr(args.schedule, "timezone", timezone)
        when = "\n".join(f"- {local_text(f, zone)}" for f in fires) or "- (never)"
        return ToolOutput(
            content=(
                f"Proposed {job.spec.kind.value} {job.id}: {job.spec.name!r}, "
                f"{describe(job.spec.schedule)}.\nNext runs:\n{when}\n"
                "It is NOT active yet: the user must confirm it in the Kairos UI."
            )
        )

    @tool(name="schedule_list")
    async def schedule_list(args: ListInput, context: ToolContext) -> ToolOutput:
        """List the user's scheduled tasks and watches with their status and next run."""
        return ToolOutput(content=summarize(await jobs.list_jobs(owner=context.user_id)))

    @tool(name="schedule_set_status", risk=ToolRisk.WRITE_SELF)
    async def schedule_set_status(args: SetStatusInput, context: ToolContext) -> ToolOutput:
        """Pause, resume or delete a scheduled job by id (see schedule_list)."""
        job = await jobs.get(args.job_id, owner=context.user_id)
        if job is None:
            return ToolOutput(content=f"no job {args.job_id}", is_error=True)
        target = JobStatus(args.status)
        if target is JobStatus.ACTIVE and job.status is not JobStatus.PAUSED:
            return ToolOutput(
                content=f"{job.id} is {job.status.value}; only a paused job can be resumed. "
                "Unconfirmed jobs must be confirmed by the user in the UI.",
                is_error=True,
            )
        now = clock.now()
        next_run = None
        if target is JobStatus.ACTIVE:
            fires = upcoming(job.spec.schedule, after=now, count=1)
            next_run = fires[0] if fires else None
        updated = await jobs.set_status(
            job.id, target, owner=context.user_id, next_run_at=next_run, now=now
        )
        status = updated.status.value if updated else target.value
        return ToolOutput(content=f"{job.id} ({job.spec.name}) is now {status}.")

    return [schedule_create, schedule_list, schedule_set_status]
