"""Live events over LISTEN/NOTIFY: what the worker publishes reaches API listeners."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import EventListener, EventPublisher
from kairos_db.notify import notify_committed
from kairos_domain.events import CheckpointWritten, StepStarted, TextMessageDelta

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 17, tzinfo=UTC)


async def test_ephemeral_events_reach_subscribers_of_that_run_only(
    engine: AsyncEngine, database_url: str
) -> None:
    listener = EventListener(database_url)
    await listener.start()
    try:
        async with (
            listener.subscribe("run_a") as inbox_a,
            listener.subscribe("run_b") as inbox_b,
            EventPublisher(engine) as publisher,
        ):
            publisher.publish(TextMessageDelta(run_id="run_a", ts=NOW, message_id="m", delta="Hi"))
            # Durable and checkpoint events are not published live.
            publisher.publish(StepStarted(run_id="run_a", ts=NOW, seq=0, step=1))
            publisher.publish(CheckpointWritten(run_id="run_a", ts=NOW, step=1, through_seq=0))

            notice = await asyncio.wait_for(inbox_a.get(), timeout=5)
            assert isinstance(notice.event, TextMessageDelta)
            assert notice.event.delta == "Hi"
            assert inbox_b.empty()

            async with engine.begin() as connection:
                await notify_committed(connection, "run_a", 7)
            committed = await asyncio.wait_for(inbox_a.get(), timeout=5)
            assert committed.through_seq == 7
            assert committed.event is None
            assert inbox_a.empty()
    finally:
        await listener.close()
