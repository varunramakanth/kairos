"""A ``Checkpointer`` backed by Postgres, bound to one lease."""

import json
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kairos_db.governance import insert_approval
from kairos_db.notify import notify_committed
from kairos_db.queue import Lease
from kairos_domain.errors import LostLeaseError
from kairos_domain.events import (
    ApprovalRequested,
    DurableEvent,
    ModelCallFinished,
    ToolCallEnd,
    ToolCallResult,
    ToolCallUncertain,
)
from kairos_domain.run import RunState, RunStatus

_SAVE = text(
    """
    UPDATE runs
       SET state = CAST(:state AS jsonb),
           status = :status,
           stop_reason = :stop_reason,
           updated_at = now(),
           lease_id = CASE WHEN :release THEN NULL ELSE lease_id END,
           lease_until = CASE WHEN :release THEN NULL
                              ELSE now() + make_interval(secs => :ttl) END
     WHERE id = :run_id AND lease_id = :lease_id
    """
)

_INSERT_EVENT = text(
    "INSERT INTO run_events (run_id, seq, type, payload) "
    "VALUES (:run_id, :seq, :type, CAST(:payload AS jsonb))"
)

_INSERT_STEP = text(
    """
    INSERT INTO run_steps (run_id, step, model, stop_reason, input_tokens, output_tokens,
                           cache_read_input_tokens, cache_creation_input_tokens, cost_usd)
    VALUES (:run_id, :step, :model, :stop_reason, :input_tokens, :output_tokens,
            :cache_read_input_tokens, :cache_creation_input_tokens, :cost_usd)
    ON CONFLICT (run_id, step) DO NOTHING
    """
)

_INSERT_TOOL_CALL = text(
    "INSERT INTO tool_calls (run_id, id, step, name, input, status) "
    "VALUES (:run_id, :id, :step, :name, CAST(:input AS jsonb), 'requested') "
    "ON CONFLICT (run_id, id) DO NOTHING"
)

_FINISH_TOOL_CALL = text(
    "UPDATE tool_calls SET status = :status, output = :output, is_error = :is_error, "
    "duration_ms = :duration_ms, finished_at = now() WHERE run_id = :run_id AND id = :id"
)

_MARK_TOOL_CALL = text("UPDATE tool_calls SET status = :status WHERE run_id = :run_id AND id = :id")


class PostgresCheckpointer:
    """Stores checkpoints for the run named by ``lease``.

    Every ``save`` is one transaction: the lease-guarded update of ``runs``, the
    event inserts and the projections. If the lease was lost the update matches no
    row, the transaction rolls back and ``LostLeaseError`` is raised, so a stale
    worker can never write state or events.
    """

    def __init__(self, engine: AsyncEngine, lease: Lease, *, lease_ttl_seconds: float) -> None:
        self._engine = engine
        self._lease = lease
        self._ttl = lease_ttl_seconds

    async def load(self, run_id: str) -> RunState | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT state FROM runs WHERE id = :run_id"), {"run_id": run_id}
                )
            ).one_or_none()
        if row is None or row.state is None:
            return None
        return RunState.model_validate(row.state)

    async def save(self, state: RunState, events: Sequence[DurableEvent]) -> None:
        # A paused or finished run gives up its lease in the same statement.
        release = state.status is not RunStatus.RUNNING
        async with self._engine.begin() as connection:
            result = await connection.execute(
                _SAVE,
                {
                    "state": state.model_dump_json(),
                    "status": state.status.value,
                    "stop_reason": state.stop_reason.value if state.stop_reason else None,
                    "release": release,
                    "ttl": self._ttl,
                    "run_id": state.run_id,
                    "lease_id": self._lease.lease_id,
                },
            )
            if result.rowcount != 1:
                raise LostLeaseError(
                    f"run {state.run_id}: lease {self._lease.lease_id} is no longer valid"
                )
            for event in events:
                payload = event.model_dump(mode="json")
                await connection.execute(
                    _INSERT_EVENT,
                    {
                        "run_id": event.run_id,
                        "seq": event.seq,
                        "type": payload["type"],
                        "payload": json.dumps(payload),
                    },
                )
                await self._project(connection, state, event)
            if events:
                # Delivered on commit, so listeners never see a seq they cannot read yet.
                await notify_committed(connection, state.run_id, events[-1].seq)

    async def put_blob(self, run_id: str, content: str) -> str:
        blob_id = f"blob_{uuid.uuid4().hex}"
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tool_result_blobs (id, run_id, content) "
                    "VALUES (:id, :run_id, :content)"
                ),
                {"id": blob_id, "run_id": run_id, "content": content},
            )
        return blob_id

    async def get_blob(self, blob_id: str) -> str | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT content FROM tool_result_blobs WHERE id = :id"), {"id": blob_id}
                )
            ).one_or_none()
        return row.content if row else None

    async def _project(
        self, connection: AsyncConnection, state: RunState, event: DurableEvent
    ) -> None:
        """Maintain the queryable tables derived from the event log."""
        run_id = event.run_id
        if isinstance(event, ModelCallFinished):
            await connection.execute(
                _INSERT_STEP,
                {
                    "run_id": run_id,
                    "step": event.step,
                    "model": event.model,
                    "stop_reason": event.stop_reason.value,
                    "cost_usd": event.cost_usd,
                    **event.usage.model_dump(),
                },
            )
        elif isinstance(event, ToolCallEnd):
            await connection.execute(
                _INSERT_TOOL_CALL,
                {
                    "run_id": run_id,
                    "id": event.tool_call_id,
                    "step": state.step,
                    "name": event.name,
                    "input": json.dumps(event.input),
                },
            )
        elif isinstance(event, ToolCallResult):
            await connection.execute(
                _FINISH_TOOL_CALL,
                {
                    "run_id": run_id,
                    "id": event.tool_call_id,
                    "status": "failed" if event.is_error else "completed",
                    "output": event.output,
                    "is_error": event.is_error,
                    "duration_ms": event.duration_ms,
                },
            )
        elif isinstance(event, ApprovalRequested):
            await insert_approval(
                connection,
                run_id=run_id,
                tool_call_id=event.tool_call_id,
                name=event.name,
                tool_input=event.input,
                reason=event.reason,
                risk=event.risk.value,
            )
        elif isinstance(event, ToolCallUncertain):
            await connection.execute(
                _MARK_TOOL_CALL, {"run_id": run_id, "id": event.tool_call_id, "status": "uncertain"}
            )
