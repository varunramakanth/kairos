"""The durable run queue: Postgres leases with a fencing token (ADR-003).

The SQL is written out by hand on purpose. The whole mechanism is three statements:
claim, heartbeat and the lease-guarded write in ``PostgresCheckpointer.save``.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kairos_domain.run import RunRequest, RunStatus, StopReason

_CLAIM = text(
    """
    UPDATE runs
       SET status = 'running',
           lease_id = :lease_id,
           lease_until = now() + make_interval(secs => :ttl),
           attempts = attempts + 1,
           updated_at = now()
     WHERE id = (
            SELECT id
              FROM runs
             WHERE status = 'queued'
                OR (status = 'running' AND lease_until < now())
             ORDER BY created_at
               FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
    RETURNING id, request, attempts
    """
)

_HEARTBEAT = text(
    """
    UPDATE runs
       SET lease_until = now() + make_interval(secs => :ttl)
     WHERE id = :run_id AND lease_id = :lease_id AND status = 'running'
    """
)

_RELEASE = text(
    """
    UPDATE runs
       SET status = 'queued', lease_id = NULL, lease_until = NULL, updated_at = now()
     WHERE id = :run_id AND lease_id = :lease_id AND status = 'running'
    """
)

_ABANDON = text(
    """
    UPDATE runs
       SET status = 'finished', stop_reason = :stop_reason,
           lease_id = NULL, lease_until = NULL, updated_at = now()
     WHERE id = :run_id AND lease_id = :lease_id
    """
)


_INSERT_RUN = text(
    "INSERT INTO runs (id, thread_id, user_id, status, request) "
    "VALUES (:id, :thread_id, :user_id, 'queued', CAST(:request AS jsonb))"
)


async def insert_run(connection: AsyncConnection, request: RunRequest) -> None:
    """Queue a run inside the caller's transaction, owned by ``request.user_id``."""
    await connection.execute(
        _INSERT_RUN,
        {
            "id": request.run_id,
            "thread_id": request.thread_id,
            "user_id": request.user_id,
            "request": request.model_dump_json(),
        },
    )


@dataclass(frozen=True)
class Lease:
    """Proof that this worker owns a run, until ``lease_until`` passes in the database."""

    run_id: str
    lease_id: str
    request: RunRequest
    attempts: int


class RunQueue:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(self, request: RunRequest) -> None:
        """Add a run. It becomes visible to workers when the transaction commits."""
        async with self._engine.begin() as connection:
            await insert_run(connection, request)

    async def claim(self, *, ttl_seconds: float) -> Lease | None:
        """Take the oldest due run, or return ``None`` if there is none.

        A run is due when it is queued, or running under a lease that has expired
        (its worker died). ``SKIP LOCKED`` makes concurrent claims pass over rows
        another transaction is claiming, so two workers never get the same run.
        """
        lease_id = uuid.uuid4().hex
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(_CLAIM, {"lease_id": lease_id, "ttl": ttl_seconds})
            ).one_or_none()
        if row is None:
            return None
        return Lease(
            run_id=row.id,
            lease_id=lease_id,
            request=RunRequest.model_validate(row.request),
            attempts=row.attempts,
        )

    async def heartbeat(self, lease: Lease, *, ttl_seconds: float) -> bool:
        """Extend the lease. ``False`` means it was lost and the worker must stop."""
        return await self._guarded(_HEARTBEAT, lease, ttl=ttl_seconds)

    async def release(self, lease: Lease) -> bool:
        """Hand a run back to the queue (graceful shutdown) without waiting for expiry."""
        return await self._guarded(_RELEASE, lease)

    async def abandon(self, lease: Lease) -> bool:
        """Stop retrying a run that keeps killing its worker."""
        return await self._guarded(_ABANDON, lease, stop_reason=StopReason.FAILED.value)

    async def resume(self, run_id: str) -> bool:
        """Put a paused run back in the queue (after approval or human input)."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE runs SET status = 'queued', updated_at = now() "
                    "WHERE id = :run_id AND status = 'paused'"
                ),
                {"run_id": run_id},
            )
        return result.rowcount == 1

    async def status(self, run_id: str) -> tuple[RunStatus, StopReason | None] | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT status, stop_reason FROM runs WHERE id = :run_id"),
                    {"run_id": run_id},
                )
            ).one_or_none()
        if row is None:
            return None
        return RunStatus(row.status), StopReason(row.stop_reason) if row.stop_reason else None

    async def _guarded(self, statement: TextClause, lease: Lease, **params: object) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                statement, {"run_id": lease.run_id, "lease_id": lease.lease_id, **params}
            )
        return result.rowcount == 1
