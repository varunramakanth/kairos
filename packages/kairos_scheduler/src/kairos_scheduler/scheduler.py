"""The scheduler loop: fire due jobs, then settle the ones whose runs finished.

Each tick:

1. **Settle.** For every job run whose agent run has stopped, decide the outcome
   (``settle``): success or failure, the watch observation, whether to alert, and
   the backoff after failures. The store records it in one transaction.
2. **Fire.** Lease each due job (``SKIP LOCKED`` plus a fencing token, ADR-003),
   compute its next run, and queue an ordinary agent run for it. A job never has
   two runs in flight, and missed firings while the scheduler was down collapse
   into one catch-up run.

All time comes from the injected ``Clock``, so tests drive it across DST changes.
"""

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from kairos_domain.jobs import Job, JobKind, JobRunStatus, JobStatus
from kairos_domain.run import RunRequest, StopReason
from kairos_domain.usage import Budget
from kairos_harness import Clock, IdGenerator, SystemClock, UuidIdGenerator
from kairos_scheduler.prompts import task_prompt, watch_prompt
from kairos_scheduler.protocols import Claim, Finished, Firing, SchedulerStore, Settlement
from kairos_scheduler.timing import describe, next_fire
from kairos_scheduler.watch import backoff, evaluate, observation_from

log = structlog.get_logger(__name__)

JOB_BUDGET = Budget(max_cost_usd=0.50, max_tool_calls=20, max_steps=15)
"""Default cap for one unattended run. A runaway job stops here, not on the bill."""


@dataclass
class TickResult:
    fired: list[str] = field(default_factory=list[str])
    expired: list[str] = field(default_factory=list[str])
    settled: int = 0


def plan_firing(job: Job, *, now: datetime, run_id: str, job_run_id: str, model: str) -> Firing:
    """What firing ``job`` now means. Missed firings collapse into this one."""
    scheduled_for = job.next_run_at or now
    following = next_fire(job.spec.schedule, after=max(scheduled_for, now))
    prompt = (
        watch_prompt(job.spec)
        if job.spec.kind is JobKind.WATCH
        else task_prompt(job.spec, now=scheduled_for)
    )
    request = RunRequest(
        run_id=run_id,
        input=prompt,
        model=job.spec.model or model,
        budget=JOB_BUDGET,
        metadata={"job_id": job.id, "job_kind": job.spec.kind.value},
    )
    return Firing(
        job_run_id=job_run_id,
        request=request,
        scheduled_for=scheduled_for,
        next_run_at=following,
        status=JobStatus.ACTIVE if following is not None else JobStatus.COMPLETED,
    )


def settle(finished: Finished, *, now: datetime) -> Settlement:
    """Judge a finished job run. Pure: everything it needs is in ``finished``."""
    job = finished.job
    observation = observation_from(finished.messages) if job.spec.kind is JobKind.WATCH else None
    error: str | None = None
    if finished.stop_reason is not StopReason.COMPLETED:
        reason = finished.stop_reason.value if finished.stop_reason else "unknown"
        error = f"run stopped: {reason}" + (f" ({finished.error})" if finished.error else "")
    elif job.spec.kind is JobKind.WATCH and observation is None:
        error = "the run did not record an observation"

    if error is not None:
        failures = job.consecutive_failures + 1
        retry_at = now + backoff(failures)
        next_run = max(job.next_run_at, retry_at) if job.next_run_at else None
        return Settlement(
            status=JobRunStatus.FAILED,
            error=error,
            observation=observation,
            matched=False,
            alert=None,
            dedupe_key=None,
            consecutive_failures=failures,
            next_run_at=next_run,
        )

    matched, alert, key = False, None, None
    if job.spec.watch is not None and observation is not None:
        verdict = evaluate(
            job.spec.watch,
            observation,
            previous=finished.previous,
            last_alert=finished.last_alert,
            now=now,
        )
        matched, alert, key = verdict.matched, verdict.alert, verdict.dedupe_key
    return Settlement(
        status=JobRunStatus.SUCCEEDED,
        error=None,
        observation=observation,
        matched=matched,
        alert=alert,
        dedupe_key=key,
        consecutive_failures=0,
        next_run_at=None,
    )


class Scheduler:
    def __init__(
        self,
        store: SchedulerStore,
        *,
        model: str,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        lease_ttl_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._model = model
        self._clock: Clock = clock or SystemClock()
        self._ids: IdGenerator = ids or UuidIdGenerator()
        self._ttl = lease_ttl_seconds

    async def tick(self) -> TickResult:
        result = TickResult()
        while await self._store.settle_next(self._decide, now=self._clock.now()):
            result.settled += 1
        while (
            claim := await self._store.claim_due(now=self._clock.now(), ttl_seconds=self._ttl)
        ) is not None:
            await self._handle(claim, result)
        return result

    def _decide(self, finished: Finished) -> Settlement:
        return settle(finished, now=self._clock.now())

    async def _handle(self, claim: Claim, result: TickResult) -> None:
        job, now = claim.job, self._clock.now()
        if job.spec.expires_at is not None and now >= job.spec.expires_at:
            if await self._store.close(claim, JobStatus.EXPIRED, now=now):
                result.expired.append(job.id)
            return
        firing = plan_firing(
            job,
            now=now,
            run_id=self._ids.new_id("run"),
            job_run_id=self._ids.new_id("jrun"),
            model=self._model,
        )
        if await self._store.fire(claim, firing, now=now):
            result.fired.append(job.id)
            log.info("job_fired", job_id=job.id, run_id=firing.request.run_id)
        else:
            log.warning("job_lease_lost", job_id=job.id)

    async def run_forever(self, stop: asyncio.Event, *, poll_seconds: float = 5.0) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler_tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


def summarize(jobs: Sequence[Job]) -> str:
    """One line per job, for the ``schedule_list`` tool."""
    if not jobs:
        return "No scheduled jobs."
    lines: list[str] = []
    for job in jobs:
        nxt = job.next_run_at.isoformat() if job.next_run_at else "none"
        lines.append(
            f"- {job.id}: {job.spec.name} [{job.spec.kind.value}, {job.status.value}] "
            f"{describe(job.spec.schedule)}; next run {nxt}"
        )
    return "\n".join(lines)
