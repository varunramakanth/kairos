"""The scheduler on Postgres, driven by a fake clock (M3).

Jobs fire through leases, at most once per slot and never while a previous run of
the same job is in flight. Watch runs execute through the real worker and agent
loop with a scripted model; the scheduler then judges the recorded observation.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import JobStore, PostgresCheckpointer, RunQueue
from kairos_domain.errors import ProviderError
from kairos_domain.jobs import (
    Below,
    CronSchedule,
    IntervalSchedule,
    JobKind,
    JobSpec,
    JobStatus,
    OneShotSchedule,
    WatchSpec,
)
from kairos_harness import AgentLoop, FakeClock, SeededIdGenerator
from kairos_harness.testing import ScriptedProvider, Turn, text_turn, tool_turn
from kairos_scheduler.scheduler import Scheduler
from kairos_scheduler.tools import record_observation_tool
from kairos_scheduler.watch import OBSERVATION_TOOL
from kairos_tools import ToolRegistry
from kairos_worker.worker import Worker

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
HOUR = 3600.0


def task(schedule: Any, **overrides: Any) -> JobSpec:
    return JobSpec.model_validate(
        {
            "name": "Morning brief",
            "kind": "task",
            "schedule": schedule,
            "prompt": "Brief me.",
            **overrides,
        }
    )


FARE_WATCH = JobSpec(
    name="YYZ-CJB fare",
    kind=JobKind.WATCH,
    schedule=IntervalSchedule(every_seconds=6 * 3600),
    watch=WatchSpec(
        target="YYZ to CJB round trip Dec 10-20",
        extract="lowest fare in CAD",
        condition=Below(threshold=1500),
        cooldown_seconds=3600,
    ),
)


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as connection:
        return list(await connection.execute(text(sql), params))


def scheduler(store: JobStore, clock: FakeClock, seed: str = "s") -> Scheduler:
    return Scheduler(store, model="claude-haiku-5-5", clock=clock, ids=SeededIdGenerator(seed))


async def execute_queued_run(engine: AsyncEngine, turns: list[Turn]) -> None:
    """Run the oldest queued run to completion through the real worker and loop."""
    tools = ToolRegistry([record_observation_tool()])
    worker = Worker(
        RunQueue(engine),
        lambda lease: AgentLoop(
            provider=ScriptedProvider(turns),
            tools=tools,
            checkpointer=PostgresCheckpointer(engine, lease, lease_ttl_seconds=60),
        ),
    )
    assert await worker.run_once() is not None


def observe(value: float) -> list[Turn]:
    return [
        tool_turn(
            ("obs", OBSERVATION_TOOL, {"found": True, "value": value, "unit": "CAD"}),
        ),
        text_turn(f"The lowest fare is {value} CAD."),
    ]


async def test_a_due_job_fires_exactly_once_under_competing_schedulers(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)
    job = await store.create(
        "job_1",
        task(IntervalSchedule(every_seconds=3600)),
        status=JobStatus.ACTIVE,
        next_run_at=NOW,
        now=NOW,
    )

    results = await asyncio.gather(*(scheduler(store, clock, f"s{i}").tick() for i in range(6)))

    assert sum(len(r.fired) for r in results) == 1
    runs = await rows(
        engine, "SELECT request->>'input' AS input, request->'metadata'->>'job_id' AS job FROM runs"
    )
    assert len(runs) == 1
    assert runs[0].job == job.id
    assert 'Scheduled task "Morning brief"' in runs[0].input
    reloaded = await store.get(job.id)
    assert reloaded is not None
    assert reloaded.next_run_at == NOW + timedelta(hours=1)


async def test_missed_firings_collapse_into_one_catch_up_run(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)  # 10:00 in Toronto
    daily = CronSchedule(expression="0 8 * * *", timezone="America/Toronto")
    three_days_ago = NOW - timedelta(days=3)
    await store.create(
        "job_1",
        task(daily),
        status=JobStatus.ACTIVE,
        next_run_at=three_days_ago,
        now=three_days_ago,
    )

    result = await scheduler(store, clock).tick()

    assert result.fired == ["job_1"]
    assert len(await rows(engine, "SELECT id FROM runs")) == 1
    job = await store.get("job_1")
    assert job is not None
    assert job.next_run_at == datetime(2026, 11, 18, 13, 0, tzinfo=UTC)  # tomorrow 08:00 EST


async def test_a_job_does_not_overlap_its_own_running_run(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)
    await store.create(
        "job_1",
        task(IntervalSchedule(every_seconds=3600)),
        status=JobStatus.ACTIVE,
        next_run_at=NOW,
        now=NOW,
    )
    sched = scheduler(store, clock)
    assert (await sched.tick()).fired == ["job_1"]

    clock.advance(2 * HOUR)  # overdue again, but the first run never ran
    assert (await sched.tick()).fired == []

    await execute_queued_run(engine, [text_turn("Here is your brief.")])
    result = await sched.tick()
    assert result.settled == 1
    assert result.fired == ["job_1"]
    statuses = await rows(engine, "SELECT status FROM job_runs ORDER BY created_at")
    assert [r.status for r in statuses] == ["succeeded", "started"]


async def test_a_watch_alerts_once_per_new_low_and_respects_dedupe(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)
    await store.create("job_w", FARE_WATCH, status=JobStatus.ACTIVE, next_run_at=NOW, now=NOW)
    sched = scheduler(store, clock)

    async def check(value: float) -> None:
        assert (await sched.tick()).fired == ["job_w"]
        await execute_queued_run(engine, observe(value))
        assert (await sched.tick()).settled == 1
        clock.advance(6 * HOUR)

    await check(1620)  # above the threshold: observed, no alert
    await check(1420)  # first time below: alert
    await check(1420)  # same price again: deduped
    await check(1380)  # a new low: alert

    detail = await store.detail("job_w")
    assert detail is not None
    assert [o.observation.value for o in reversed(detail.observations)] == [1620, 1420, 1420, 1380]
    assert [o.matched for o in reversed(detail.observations)] == [False, True, True, True]
    assert [a.message for a in reversed(detail.alerts)] == [
        "lowest fare in CAD is 1420 CAD, below 1500",
        "lowest fare in CAD is 1380 CAD, below 1500",
    ]
    summaries = await store.summaries()
    assert summaries[0].recent_values == [1620, 1420, 1420, 1380]
    assert summaries[0].alert_count == 2


async def test_a_watch_run_without_an_observation_fails_and_backs_off(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)
    spec = FARE_WATCH.model_copy(update={"schedule": IntervalSchedule(every_seconds=300)})
    await store.create("job_w", spec, status=JobStatus.ACTIVE, next_run_at=NOW, now=NOW)
    sched = scheduler(store, clock)

    await sched.tick()
    await execute_queued_run(engine, [text_turn("I could not check.")])
    await sched.tick()
    await sched.tick()  # settle happens first; nothing is due during backoff

    job = await store.get("job_w")
    assert job is not None
    assert job.consecutive_failures == 1
    assert job.next_run_at == NOW + timedelta(minutes=5)
    (run,) = await rows(engine, "SELECT status, error FROM job_runs")
    assert run.status == "failed"
    assert run.error == "the run did not record an observation"

    clock.advance(300)
    assert (await sched.tick()).fired == ["job_w"]
    await execute_queued_run(engine, [ProviderError("overloaded", retryable=False)])
    clock.advance(1)
    await sched.tick()
    job = await store.get("job_w")
    assert job is not None
    assert job.consecutive_failures == 2
    assert job.next_run_at is not None
    assert job.next_run_at >= clock.now() + timedelta(minutes=10) - timedelta(seconds=1)


async def test_one_shot_jobs_complete_and_expired_jobs_stop(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)
    await store.create(
        "job_once", task(OneShotSchedule(at=NOW)), status=JobStatus.ACTIVE, next_run_at=NOW, now=NOW
    )
    await store.create(
        "job_old",
        task(IntervalSchedule(every_seconds=3600), expires_at=NOW - timedelta(minutes=1)),
        status=JobStatus.ACTIVE,
        next_run_at=NOW,
        now=NOW,
    )

    result = await scheduler(store, clock).tick()

    assert result.fired == ["job_once"]
    assert result.expired == ["job_old"]
    once, old = await store.get("job_once"), await store.get("job_old")
    assert once is not None
    assert old is not None
    assert (once.status, once.next_run_at) == (JobStatus.COMPLETED, None)
    assert old.status is JobStatus.EXPIRED


async def test_paused_and_unconfirmed_jobs_never_fire(engine: AsyncEngine) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)
    every_hour = IntervalSchedule(every_seconds=3600)
    await store.create("job_p", task(every_hour), status=JobStatus.PAUSED, next_run_at=NOW, now=NOW)
    await store.create(
        "job_u", task(every_hour), status=JobStatus.PENDING_CONFIRMATION, next_run_at=NOW, now=NOW
    )
    await store.create("job_a", task(every_hour), status=JobStatus.ACTIVE, next_run_at=NOW, now=NOW)

    assert await store.pause_all(now=NOW) == 1
    assert (await scheduler(store, clock).tick()).fired == []


async def test_a_cron_job_follows_the_wall_clock_through_spring_forward(
    engine: AsyncEngine,
) -> None:
    clock = FakeClock(datetime(2026, 3, 7, 17, 0, tzinfo=UTC))  # Sat 12:00 EST
    store = JobStore(engine)
    daily = CronSchedule(expression="0 8 * * *", timezone="America/Toronto")
    await store.create(
        "job_1",
        task(daily),
        status=JobStatus.ACTIVE,
        next_run_at=datetime(2026, 3, 8, 12, 0, tzinfo=UTC),
        now=clock.now(),
    )
    sched = scheduler(store, clock)

    clock.advance(18 * HOUR)  # Sun 07:00 EDT (11:00 UTC): not yet
    assert (await sched.tick()).fired == []
    clock.advance(HOUR)  # Sun 08:00 EDT
    assert (await sched.tick()).fired == ["job_1"]
    job = await store.get("job_1")
    assert job is not None
    assert job.next_run_at == datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
