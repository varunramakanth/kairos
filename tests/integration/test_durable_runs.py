"""Durable runs on Postgres: leases, checkpoints, crash recovery (ADR-003).

Lease expiry is simulated by moving ``lease_until`` into the past in SQL. Nothing
here sleeps on the real clock.
"""

import asyncio
from collections.abc import Sequence
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import Lease, PostgresCheckpointer, RunQueue
from kairos_domain.errors import LostLeaseError
from kairos_domain.run import RunRequest, RunState, RunStatus, StopReason, ToolCallRecord
from kairos_domain.tools import ToolContext, ToolSpec
from kairos_harness import AgentLoop, Harness, SeededIdGenerator
from kairos_harness.testing import (
    ScriptedProvider,
    StaticToolExecutor,
    ToolFn,
    Turn,
    simple_spec,
    text_turn,
    tool_turn,
)
from kairos_worker.worker import Worker

pytestmark = pytest.mark.integration

TTL = 60.0


def request(run_id: str = "run_1") -> RunRequest:
    return RunRequest(run_id=run_id, input="hello", model="claude-opus-5-5")


async def echo(call: ToolCallRecord, context: ToolContext) -> str:
    return "echo"


def loop_for(
    engine: AsyncEngine,
    lease: Lease,
    turns: Sequence[Turn] | ScriptedProvider,
    tools: Sequence[tuple[ToolSpec, ToolFn]] = (),
) -> AgentLoop:
    provider = turns if isinstance(turns, ScriptedProvider) else ScriptedProvider(turns)
    return AgentLoop(
        provider=provider,
        tools=StaticToolExecutor(tools),
        checkpointer=PostgresCheckpointer(engine, lease, lease_ttl_seconds=TTL),
        ids=SeededIdGenerator(),
    )


async def expire_lease(engine: AsyncEngine, run_id: str) -> None:
    """What the database looks like after the owning worker has been dead for a while."""
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": run_id},
        )


async def scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql))).scalar_one()


async def claim(queue: RunQueue) -> Lease:
    lease = await queue.claim(ttl_seconds=TTL)
    assert lease is not None
    return lease


# --- leases ---------------------------------------------------------------------------


async def test_a_run_is_claimed_by_exactly_one_of_many_concurrent_workers(
    engine: AsyncEngine,
) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())

    leases = await asyncio.gather(*(queue.claim(ttl_seconds=TTL) for _ in range(8)))

    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].run_id == "run_1"
    assert winners[0].attempts == 1


async def test_runs_are_claimed_oldest_first_and_only_when_due(engine: AsyncEngine) -> None:
    queue = RunQueue(engine)
    for run_id in ("run_a", "run_b"):
        await queue.enqueue(request(run_id))

    first, second = await claim(queue), await claim(queue)
    assert [first.run_id, second.run_id] == ["run_a", "run_b"]
    assert await queue.claim(ttl_seconds=TTL) is None  # both are running under live leases

    await expire_lease(engine, "run_b")
    retaken = await claim(queue)
    assert retaken.run_id == "run_b"
    assert retaken.lease_id != second.lease_id  # a fresh fencing token
    assert retaken.attempts == 2


async def test_heartbeat_extends_only_a_lease_that_is_still_held(engine: AsyncEngine) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())
    stale = await claim(queue)
    assert await queue.heartbeat(stale, ttl_seconds=TTL)

    await expire_lease(engine, "run_1")
    current = await claim(queue)

    assert not await queue.heartbeat(stale, ttl_seconds=TTL)
    assert await queue.heartbeat(current, ttl_seconds=TTL)


# --- checkpoints ----------------------------------------------------------------------


async def test_completed_run_is_persisted_with_events_and_projections(
    engine: AsyncEngine,
) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())
    lease = await claim(queue)
    loop = loop_for(
        engine,
        lease,
        [tool_turn(("t1", "echo", {"q": "x"})), text_turn("all done")],
        [(simple_spec("echo"), echo)],
    )

    events = [e async for e in loop.run(lease.request)]

    assert await queue.status("run_1") == (RunStatus.FINISHED, StopReason.COMPLETED)
    durable = [e.seq for e in events if e.seq is not None]
    assert await scalar(engine, "SELECT count(*) FROM run_events") == len(durable)
    assert await scalar(engine, "SELECT max(seq) FROM run_events") == durable[-1]
    assert await scalar(engine, "SELECT count(*) FROM run_steps") == 2
    assert await scalar(engine, "SELECT status FROM tool_calls WHERE id = 't1'") == "completed"
    assert await scalar(engine, "SELECT output FROM tool_calls WHERE id = 't1'") == "echo"
    # A finished run gives up its lease.
    assert await scalar(engine, "SELECT lease_id FROM runs") is None

    state = await PostgresCheckpointer(engine, lease, lease_ttl_seconds=TTL).load("run_1")
    assert isinstance(state, RunState)
    assert state.output_text == "all done"
    assert len(state.messages) == 4


async def test_stale_worker_cannot_write_after_losing_its_lease(engine: AsyncEngine) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())
    stale = await claim(queue)
    ran: list[str] = []

    async def side_effect(call: ToolCallRecord, context: ToolContext) -> str:
        ran.append(call.id)
        return "sent"

    loop = loop_for(
        engine,
        stale,
        [tool_turn(("t1", "send", {}))],
        [(simple_spec("send", idempotent=False), side_effect)],
    )

    with pytest.raises(LostLeaseError):
        async for event in loop.run(stale.request):
            if event.type == "model_call_finished":
                # The worker stalls; its lease expires and another worker takes the run.
                await expire_lease(engine, "run_1")
                await claim(queue)

    assert ran == []  # the guard fired before the side effect
    events_before = await scalar(engine, "SELECT count(*) FROM run_events")
    # Nothing from the failed save was written: the transaction rolled back.
    assert await scalar(engine, "SELECT count(*) FROM tool_calls") == 0
    assert events_before == 1  # only run_started, from the first checkpoint


# --- crash and resume through the worker ----------------------------------------------


class ProcessDiedError(BaseException):
    """Stands in for the worker process being killed."""


async def test_worker_resumes_a_crashed_run_from_its_checkpoint(engine: AsyncEngine) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())
    attempts: list[str] = []

    async def flaky_fetch(call: ToolCallRecord, context: ToolContext) -> str:
        attempts.append(context.idempotency_key)
        if len(attempts) == 1:
            raise ProcessDiedError
        return "page"

    tools = [(simple_spec("fetch", idempotent=True), flaky_fetch)]
    providers = [
        ScriptedProvider([tool_turn(("t1", "fetch", {}))]),
        ScriptedProvider([text_turn("recovered")]),
    ]
    pending = list(providers)

    def build(lease: Lease) -> Harness:
        return loop_for(engine, lease, pending.pop(0), tools)

    worker = Worker(queue, build, lease_ttl_seconds=TTL)

    with pytest.RaisesGroup(ProcessDiedError):
        await worker.run_once()
    assert await queue.status("run_1") == (RunStatus.RUNNING, None)
    assert await worker.run_once() is None  # still leased to the dead worker

    await expire_lease(engine, "run_1")
    assert await worker.run_once() == "run_1"

    assert await queue.status("run_1") == (RunStatus.FINISHED, StopReason.COMPLETED)
    assert attempts == ["t1", "t1"]  # re-run with the same idempotency key
    # The resumed process saw the full history, including the first model turn.
    assert [m.role for m in providers[1].requests[0].messages] == ["user", "assistant", "user"]
    assert await scalar(engine, "SELECT attempts FROM runs") == 2
    seqs = cast(
        "list[int]", await scalar(engine, "SELECT array_agg(seq ORDER BY seq) FROM run_events")
    )
    assert seqs == list(range(len(seqs)))  # no gaps or duplicates across the crash


async def test_uncertain_side_effect_pauses_the_run_until_it_is_resumed(
    engine: AsyncEngine,
) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())

    async def die(call: ToolCallRecord, context: ToolContext) -> str:
        raise ProcessDiedError

    tools = [(simple_spec("send_email", idempotent=False), die)]
    scripts: list[list[Turn]] = [[tool_turn(("t1", "send_email", {}))], []]
    worker = Worker(queue, lambda lease: loop_for(engine, lease, scripts.pop(0), tools))

    with pytest.RaisesGroup(ProcessDiedError):
        await worker.run_once()
    await expire_lease(engine, "run_1")
    await worker.run_once()

    assert await queue.status("run_1") == (RunStatus.PAUSED, StopReason.AWAITING_INPUT)
    assert await scalar(engine, "SELECT status FROM tool_calls WHERE id = 't1'") == "uncertain"
    assert await queue.claim(ttl_seconds=TTL) is None  # paused runs are not claimable
    assert await queue.resume("run_1")
    assert (await claim(queue)).attempts == 3


async def test_graceful_shutdown_hands_the_run_back_to_the_queue(engine: AsyncEngine) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())
    started, stop = asyncio.Event(), asyncio.Event()

    async def slow(call: ToolCallRecord, context: ToolContext) -> str:
        started.set()
        await asyncio.Event().wait()  # never finishes on its own
        return "unreachable"

    worker = Worker(
        queue,
        lambda lease: loop_for(
            engine, lease, [tool_turn(("t1", "slow", {}))], [(simple_spec("slow"), slow)]
        ),
        lease_ttl_seconds=TTL,
    )
    running = asyncio.create_task(worker.run_forever(stop))
    await asyncio.wait_for(started.wait(), timeout=5)

    stop.set()
    await asyncio.wait_for(running, timeout=5)

    # Released, not left to expire: the next worker can take it immediately.
    assert await queue.status("run_1") == (RunStatus.QUEUED, None)
    assert (await claim(queue)).attempts == 2


async def test_a_run_that_keeps_killing_workers_is_abandoned(engine: AsyncEngine) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(request())
    worker = Worker(queue, lambda lease: loop_for(engine, lease, []), max_attempts=2)
    for _ in range(2):
        await claim(queue)
        await expire_lease(engine, "run_1")

    assert await worker.run_once() == "run_1"  # third attempt: over the limit
    assert await queue.status("run_1") == (RunStatus.FINISHED, StopReason.FAILED)
