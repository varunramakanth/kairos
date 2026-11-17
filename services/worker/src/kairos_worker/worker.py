"""The worker: claims due runs and drives each one through a harness."""

import asyncio
from collections.abc import Callable
from contextlib import suppress

import structlog

from kairos_db import Lease, RunQueue
from kairos_domain.errors import LostLeaseError
from kairos_domain.events import RunEvent
from kairos_harness import Harness

log = structlog.get_logger(__name__)


class Worker:
    """Runs one claimed run at a time.

    Guarantees, together with the lease-guarded checkpointer:

    - A run is executed by at most one worker at a time.
    - If this process dies, the lease expires and another worker resumes the run
      from its last checkpoint.
    - On graceful shutdown the current run is handed back to the queue.
    """

    def __init__(
        self,
        queue: RunQueue,
        build_harness: Callable[[Lease], Harness],
        *,
        lease_ttl_seconds: float = 60.0,
        poll_seconds: float = 1.0,
        max_attempts: int = 5,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self._queue = queue
        self._build_harness = build_harness
        self._ttl = lease_ttl_seconds
        self._poll = poll_seconds
        self._max_attempts = max_attempts
        self._on_event = on_event

    async def run_once(self) -> str | None:
        """Claim and execute one run. Returns its id, or ``None`` if nothing was due."""
        lease = await self._queue.claim(ttl_seconds=self._ttl)
        if lease is None:
            return None
        if lease.attempts > self._max_attempts:
            # The run has taken down its worker repeatedly. Stop feeding it workers.
            await self._queue.abandon(lease)
            log.error("run_abandoned", run_id=lease.run_id, attempts=lease.attempts)
            return lease.run_id
        await self._execute(lease)
        return lease.run_id

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll for work until ``stop`` is set, then shut down cleanly."""
        while not stop.is_set():
            work = asyncio.create_task(self.run_once())
            stopping = asyncio.create_task(stop.wait())
            await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            if not work.done():
                work.cancel()  # _execute releases the lease on cancellation
                with suppress(asyncio.CancelledError):
                    await work
                return
            try:
                idle = work.result() is None
            except Exception:
                # Database hiccup or a bug outside the loop. The lease will expire
                # and the run will be retried; keep the worker alive.
                log.exception("worker_iteration_failed")
                idle = True
            if idle:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)

    async def _execute(self, lease: Lease) -> None:
        harness = self._build_harness(lease)
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            async for event in harness.run(lease.request):
                if self._on_event:
                    self._on_event(event)
        except LostLeaseError:
            log.warning("lease_lost", run_id=lease.run_id, lease_id=lease.lease_id)
        except asyncio.CancelledError:
            await asyncio.shield(self._queue.release(lease))
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, lease: Lease) -> None:
        """Keep the lease alive during long model or tool calls."""
        while True:
            await asyncio.sleep(self._ttl / 3)
            if not await self._queue.heartbeat(lease, ttl_seconds=self._ttl):
                return
