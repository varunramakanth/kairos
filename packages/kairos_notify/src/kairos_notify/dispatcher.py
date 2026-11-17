"""The notification outbox dispatcher.

Producers never send directly: they insert a notification in the same transaction
as whatever caused it (an alert, an approval request). The dispatcher claims
pending rows (``SKIP LOCKED``), sends them on each requested channel, and records
the outcome. A crash between sending and recording can repeat a message; it can
never lose one.
"""

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Protocol

import structlog

from kairos_domain.governance import StoredNotification
from kairos_harness import Clock, SystemClock
from kairos_notify.channels import Channel, ChannelError

log = structlog.get_logger(__name__)
MAX_ATTEMPTS = 5


class Outbox(Protocol):
    async def claim(
        self, *, now: datetime, limit: int, ttl_seconds: float
    ) -> Sequence[StoredNotification]: ...

    async def finish(
        self,
        notification_id: int,
        *,
        results: Mapping[str, str],
        sent: bool,
        retry_at: datetime | None,
        now: datetime,
    ) -> None:
        """Record the outcome. ``retry_at`` set means try again then; otherwise the
        notification is final (sent, or failed for good)."""
        ...


def retry_delay(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** max(attempts - 1, 0), 60))


class Dispatcher:
    def __init__(
        self,
        outbox: Outbox,
        channels: Sequence[Channel],
        *,
        clock: Clock | None = None,
        ttl_seconds: float = 120.0,
    ) -> None:
        self._outbox = outbox
        self._channels = {channel.name: channel for channel in channels}
        self._clock: Clock = clock or SystemClock()
        self._ttl = ttl_seconds

    async def tick(self, *, limit: int = 20) -> int:
        batch = await self._outbox.claim(now=self._clock.now(), limit=limit, ttl_seconds=self._ttl)
        for item in batch:
            await self._deliver(item)
        return len(batch)

    async def _deliver(self, item: StoredNotification) -> None:
        results: dict[str, str] = dict(item.results)
        retry = False
        for name in item.notification.channels:
            if results.get(name) == "sent":
                continue  # already delivered on an earlier attempt
            channel = self._channels.get(name)
            if channel is None:
                results[name] = "skipped (not configured)"
                continue
            try:
                await channel.send(item.notification)
                results[name] = "sent"
            except ChannelError as error:
                results[name] = f"error: {error}"
                retry = retry or error.retryable
            except Exception as error:
                log.exception("channel_failed", channel=name, notification_id=item.id)
                results[name] = f"error: {type(error).__name__}"
                retry = True
        attempts = item.attempts + 1
        sent = any(value == "sent" for value in results.values())
        retry_at = None
        if retry and attempts < MAX_ATTEMPTS:
            retry_at = self._clock.now() + retry_delay(attempts)
        await self._outbox.finish(
            item.id,
            results=results,
            sent=sent and retry_at is None,
            retry_at=retry_at,
            now=self._clock.now(),
        )

    async def run_forever(self, stop: asyncio.Event, *, poll_seconds: float = 2.0) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("dispatcher_tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
