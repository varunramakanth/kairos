"""Live run events over Postgres ``LISTEN/NOTIFY``.

Durable events live in ``run_events`` and are the source of truth. This module only
tells listeners that something happened, so they do not have to poll tightly:

- ``PostgresCheckpointer.save`` sends ``{"run_id", "through_seq"}`` inside its
  transaction, so the notice is delivered exactly when the events are committed.
- ``EventPublisher`` (in the worker) sends ephemeral events such as text deltas,
  which are never stored. Missing one is harmless: the durable ``*_end`` event that
  follows carries the full value.

Notices are best effort. Readers must also re-read ``run_events`` on a timer.
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

import asyncpg
import structlog
from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kairos_domain.base import DomainModel
from kairos_domain.events import CheckpointWritten, EphemeralEvent, RunEvent

CHANNEL = "kairos_run_events"
MAX_PAYLOAD_BYTES = 7900
"""Postgres rejects NOTIFY payloads of 8000 bytes or more."""

_NOTIFY = text("SELECT pg_notify(:channel, :payload)")
_EVENT = TypeAdapter[RunEvent](RunEvent)
log = structlog.get_logger(__name__)


class Notice(DomainModel):
    """One notification. Either durable events up to ``through_seq`` were committed,
    or ``event`` is a live-only event."""

    run_id: str
    through_seq: int | None = None
    event: RunEvent | None = None


async def notify_committed(connection: AsyncConnection, run_id: str, through_seq: int) -> None:
    """Announce stored events. Call inside the transaction that inserts them."""
    payload = Notice(run_id=run_id, through_seq=through_seq).model_dump_json()
    await connection.execute(_NOTIFY, {"channel": CHANNEL, "payload": payload})


def asyncpg_dsn(database_url: str) -> str:
    """The plain ``postgresql://`` DSN asyncpg expects, from a SQLAlchemy URL."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class EventPublisher:
    """Sends ephemeral run events to listeners without slowing the agent loop.

    ``publish`` only queues; a background task sends queued events in small
    batches, one transaction each. If the queue is full, events are dropped.
    """

    def __init__(self, engine: AsyncEngine, *, max_queue: int = 10_000) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "EventPublisher":
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._queue.join()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def publish(self, event: RunEvent) -> None:
        if not isinstance(event, EphemeralEvent) or isinstance(event, CheckpointWritten):
            return
        payload = Notice(run_id=event.run_id, event=event).model_dump_json()
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            return
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(payload)

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty() and len(batch) < 100:
                batch.append(self._queue.get_nowait())
            try:
                async with self._engine.begin() as connection:
                    for payload in batch:
                        await connection.execute(_NOTIFY, {"channel": CHANNEL, "payload": payload})
            except Exception:
                log.exception("event_publish_failed", dropped=len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()


class EventListener:
    """One ``LISTEN`` connection per process, fanned out to per-run subscribers."""

    def __init__(self, database_url: str, *, max_queue: int = 10_000) -> None:
        self._dsn = asyncpg_dsn(database_url)
        self._max_queue = max_queue
        self._connection: asyncpg.Connection | None = None
        self._subscribers: defaultdict[str, set[asyncio.Queue[Notice]]] = defaultdict(set)

    async def start(self) -> None:
        self._connection = await asyncpg.connect(self._dsn)
        await self._connection.add_listener(CHANNEL, self._on_notify)

    async def close(self) -> None:
        if self._connection is not None:
            with suppress(Exception):
                await self._connection.close()
            self._connection = None

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncGenerator[asyncio.Queue[Notice]]:
        """Receive notices for one run while the context is open."""
        queue: asyncio.Queue[Notice] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers[run_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[run_id].discard(queue)
            if not self._subscribers[run_id]:
                del self._subscribers[run_id]

    def _on_notify(self, connection: Any, pid: int, channel: str, payload: object) -> None:
        try:
            data = json.loads(str(payload))
            notice = Notice(
                run_id=data["run_id"],
                through_seq=data.get("through_seq"),
                event=_EVENT.validate_python(data["event"]) if data.get("event") else None,
            )
        except Exception:
            log.warning("bad_notice", payload=str(payload)[:200])
            return
        for queue in self._subscribers.get(notice.run_id, ()):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(notice)
