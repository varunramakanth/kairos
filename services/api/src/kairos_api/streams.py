"""The run event stream behind ``GET /runs/{id}/events``.

Durable events are read from ``run_events``, so a client that reconnects with the
last ``seq`` it saw gets exactly what it missed. Ephemeral events (text deltas,
tool-argument deltas) arrive live through ``LISTEN/NOTIFY`` and are forwarded as
they come; they are never replayed.

The stream ends after a ``run_finished`` event, or at once if the run is not
active and the client has already seen everything.
"""

import asyncio
from collections.abc import AsyncIterator

from kairos_db import EventListener, Notice, RunReader
from kairos_domain.events import RunEvent, RunFinished


async def run_event_stream(
    reader: RunReader,
    listener: EventListener,
    run_id: str,
    *,
    after_seq: int = -1,
    poll_seconds: float = 2.0,
) -> AsyncIterator[RunEvent]:
    """Yield the run's events after ``after_seq``, then live events until it stops.

    The subscription opens before the first read so that nothing committed in
    between is missed. Polling at ``poll_seconds`` keeps the stream correct even if
    a notification is lost.
    """
    async with listener.subscribe(run_id) as inbox:
        last = after_seq
        while True:
            backlog = await reader.events_after(run_id, last)
            for event in backlog:
                last = event.seq if event.seq is not None else last
                yield event
                if isinstance(event, RunFinished):
                    return
            if not backlog and not await reader.is_active(run_id):
                # The run may have finished between the read above and the status
                # check. Status and events commit together, so one more read is
                # complete.
                for event in await reader.events_after(run_id, last):
                    yield event
                return
            try:
                first = await asyncio.wait_for(inbox.get(), timeout=poll_seconds)
            except TimeoutError:
                continue
            for notice in _drain(first, inbox):
                if notice.event is not None:
                    yield notice.event


def _drain(first: Notice, inbox: asyncio.Queue[Notice]) -> list[Notice]:
    notices = [first]
    while not inbox.empty():
        notices.append(inbox.get_nowait())
    return notices
