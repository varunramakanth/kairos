"""PLAN.md signature scenario 1, end to end: a fare watch from proposal to alerts.

"Watch Toronto -> Coimbatore round-trip fares for Dec 10-20, alert me under $1,200 CAD,
check every 6 hours": the agent proposes the watch, the user confirms it, the scheduler
fires it on a fake clock, each firing searches flights and records the lowest fare,
and code (not the model) raises one alert per new low, with no duplicates. Alerts go
to the notification outbox without an approval.

The model turns are scripted and the flight provider serves prepared fares; the job
store, scheduler, worker, leases, tools and outbox are the real ones on Postgres.
"""

from typing import Any

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import JobStore, PostgresCheckpointer, RunQueue
from kairos_domain.jobs import JobStatus
from kairos_domain.run import RunRequest
from kairos_domain.users import LOCAL_USER
from kairos_harness import AgentLoop, FakeClock, InMemoryCheckpointer
from kairos_harness.testing import ScriptedProvider, Turn, text_turn, tool_turn
from kairos_policy import PolicyHooks
from kairos_scheduler.timing import next_fire
from kairos_scheduler.tools import record_observation_tool, schedule_tools
from kairos_scheduler.watch import OBSERVATION_TOOL
from kairos_tools import ToolRegistry
from kairos_tools.flights import StaticFlights, flights_search_tool
from kairos_worker.worker import Worker
from tests.integration.test_scheduler import NOW, rows, scheduler

pytestmark = pytest.mark.integration

HOURS_6 = 6 * 3600.0
WATCH: dict[str, JsonValue] = {
    "name": "YYZ-CJB fare, Dec 10-20",
    "kind": "watch",
    "schedule": {"kind": "interval", "every_seconds": 21600},
    "watch": {
        "target": "Round trip YYZ to CJB, depart 2026-12-10, return 2026-12-20, 1 adult",
        "extract": "lowest round-trip fare in CAD",
        "condition": {"kind": "below", "threshold": 1200},
        "cooldown_seconds": 3600,
    },
}
SEARCH: dict[str, JsonValue] = {
    "origin": "YYZ",
    "destination": "CJB",
    "outbound_date": "2026-12-10",
    "return_date": "2026-12-20",
}


def firing(price: float) -> list[Turn]:
    """One watch run: search, then record the lowest fare the search reported."""
    return [
        tool_turn(("search", "flights_search", SEARCH)),
        tool_turn(
            ("obs", OBSERVATION_TOOL, {"found": True, "value": price, "unit": "CAD"}),
        ),
        text_turn(f"Lowest round-trip fare: {price:.0f} CAD."),
    ]


async def test_a_fare_watch_alerts_once_per_new_low_from_proposal_to_outbox(
    engine: AsyncEngine,
) -> None:
    store, clock = JobStore(engine), FakeClock(NOW)

    # 1. Chat: the agent proposes the watch. It does nothing until confirmed.
    chat = AgentLoop(
        provider=ScriptedProvider(
            [tool_turn(("p1", "schedule_create", WATCH)), text_turn("Proposed; please confirm.")]
        ),
        tools=ToolRegistry(schedule_tools(store, clock)),
        checkpointer=InMemoryCheckpointer(),
        hooks=PolicyHooks(),  # schedule_create is a proposal: no approval prompt
    )
    _ = [e async for e in chat.run(RunRequest(run_id="chat", input="Watch fares", model="m"))]
    (job,) = await store.list_jobs(owner=LOCAL_USER)
    assert job.status is JobStatus.PENDING_CONFIRMATION
    sched = scheduler(store, clock)
    assert (await sched.tick()).fired == []  # unconfirmed jobs never fire

    # 2. The user confirms (what POST /jobs/{id}/confirm does).
    await store.set_status(
        job.id,
        JobStatus.ACTIVE,
        next_run_at=next_fire(job.spec.schedule, after=clock.now()),
        now=clock.now(),
        owner=LOCAL_USER,
    )

    # 3. Five firings, six hours apart, through the real scheduler, worker and outbox.
    fares = [1350.0, 1180.0, 1180.0, 1150.0, 1210.0]
    provider = StaticFlights(fares, fetched_at=NOW)
    tools = ToolRegistry([flights_search_tool(provider), record_observation_tool()])
    for fare in fares:
        clock.advance(HOURS_6)
        assert (await sched.tick()).fired == [job.id]
        worker = Worker(
            RunQueue(engine),
            lambda lease, fare=fare: AgentLoop(
                provider=ScriptedProvider(firing(fare)),
                tools=tools,
                checkpointer=PostgresCheckpointer(engine, lease, lease_ttl_seconds=60),
            ),
        )
        assert await worker.run_once() is not None
        assert (await sched.tick()).settled == 1

    # Every firing searched the same trip.
    assert {(q.origin, q.destination, str(q.return_date)) for q in provider.queries} == {
        ("YYZ", "CJB", "2026-12-20")
    }
    detail = await store.detail(job.id, owner=LOCAL_USER)
    assert detail is not None
    assert [o.observation.value for o in reversed(detail.observations)] == fares
    # One alert when it first drops below $1,200, one for the new low; none for the
    # repeated 1,180 or the rise back above the threshold.
    assert [a.message for a in reversed(detail.alerts)] == [
        "lowest round-trip fare in CAD is 1180 CAD, below 1200",
        "lowest round-trip fare in CAD is 1150 CAD, below 1200",
    ]
    queued: list[Any] = await rows(engine, "SELECT kind, payload FROM notifications ORDER BY id")
    assert [r.kind for r in queued] == ["alert", "alert"]
    assert "1150" in str(queued[1].payload)
    approvals: list[Any] = await rows(engine, "SELECT id FROM approvals")
    assert approvals == []  # system alerts never wait for approval
