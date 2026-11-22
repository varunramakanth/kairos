"""Threads and the read side of runs: what the API and the UI query."""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kairos_db.queue import insert_run
from kairos_domain.content import Message
from kairos_domain.errors import KairosError
from kairos_domain.events import RunEvent
from kairos_domain.run import RunRequest, RunState
from kairos_domain.threads import RunDetail, RunSummary, Thread, ThreadDetail

_EVENTS = TypeAdapter[list[RunEvent]](list[RunEvent])
_MESSAGES = TypeAdapter[list[Message]](list[Message])

_RUN_COLUMNS = """
    id, thread_id, status, stop_reason, created_at,
    request->>'input' AS input,
    request->>'model' AS model,
    state->>'output_text' AS output_text,
    state->>'error' AS error,
    COALESCE((state->>'cost_usd')::float, 0) AS cost_usd
"""


class ThreadBusyError(KairosError):
    """The thread already has a run queued or in progress."""


class NotFoundError(KairosError):
    pass


def continuable_history(messages: Sequence[Message]) -> tuple[Message, ...]:
    """The longest prefix of ``messages`` that ends with a complete assistant turn.

    A run that failed or paused can end with a tool call that has no result, or with
    a user message nobody answered. The next turn continues from the last point where
    the assistant had finished speaking; nothing before that point is changed.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and not message.tool_uses:
            return tuple(messages[: index + 1])
    return ()


def _summary(row: RowMapping) -> RunSummary:
    return RunSummary.model_validate(dict(row))


class ThreadRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, thread_id: str, title: str, *, owner: str) -> Thread:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "INSERT INTO threads (id, title, user_id) VALUES (:id, :title, :owner) "
                            "RETURNING id, title, created_at, updated_at"
                        ),
                        {"id": thread_id, "title": title, "owner": owner},
                    )
                )
                .mappings()
                .one()
            )
        return Thread.model_validate(dict(row))

    async def recent(self, *, owner: str, limit: int = 50) -> list[Thread]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, title, created_at, updated_at FROM threads "
                        "WHERE user_id = :owner ORDER BY updated_at DESC LIMIT :limit"
                    ),
                    {"owner": owner, "limit": limit},
                )
            ).mappings()
            return [Thread.model_validate(dict(row)) for row in rows]

    async def get(self, thread_id: str, *, owner: str) -> ThreadDetail:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, title, created_at, updated_at FROM threads "
                            "WHERE id = :id AND user_id = :owner"
                        ),
                        {"id": thread_id, "owner": owner},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFoundError(f"thread {thread_id} not found")
            runs = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_RUN_COLUMNS} FROM runs "  # noqa: S608 (constant columns)
                            "WHERE thread_id = :id ORDER BY created_at"
                        ),
                        {"id": thread_id},
                    )
                )
                .mappings()
                .all()
            )
            details = [
                RunDetail(run=_summary(run), events=await _events(connection, run["id"]))
                for run in runs
            ]
        return ThreadDetail(thread=Thread.model_validate(dict(row)), runs=details)

    async def start_turn(
        self, thread_id: str, build: Callable[[tuple[Message, ...]], RunRequest], *, owner: str
    ) -> RunRequest:
        """Queue the next run of a thread, built on the thread's history.

        The thread row is locked for the duration, so two messages sent at once
        cannot both start a run, and the history read is the one the run gets.
        """
        async with self._engine.begin() as connection:
            locked = (
                await connection.execute(
                    text(
                        "SELECT title FROM threads WHERE id = :id AND user_id = :owner FOR UPDATE"
                    ),
                    {"id": thread_id, "owner": owner},
                )
            ).one_or_none()
            if locked is None:
                raise NotFoundError(f"thread {thread_id} not found")
            latest = (
                await connection.execute(
                    text(
                        "SELECT status, state FROM runs WHERE thread_id = :id "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"id": thread_id},
                )
            ).one_or_none()
            if latest is not None and latest.status in ("queued", "running"):
                raise ThreadBusyError(f"thread {thread_id} already has a run in progress")
            history: tuple[Message, ...] = ()
            if latest is not None and latest.state is not None:
                history = continuable_history(RunState.model_validate(latest.state).messages)
            request = build(history)
            if request.user_id != owner:
                raise ValueError("a thread's runs must belong to the thread's owner")
            await insert_run(connection, request)
            await connection.execute(
                text("UPDATE threads SET updated_at = now() WHERE id = :id"), {"id": thread_id}
            )
        return request


class RunReader:
    """Read access to runs and their event logs."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, run_id: str, *, owner: str) -> RunDetail:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_RUN_COLUMNS} FROM runs "  # noqa: S608
                            "WHERE id = :id AND user_id = :owner"
                        ),
                        {"id": run_id, "owner": owner},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFoundError(f"run {run_id} not found")
            return RunDetail(run=_summary(row), events=await _events(connection, run_id))

    async def exists(self, run_id: str, *, owner: str) -> bool:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT 1 FROM runs WHERE id = :id AND user_id = :owner"),
                    {"id": run_id, "owner": owner},
                )
            ).one_or_none()
        return row is not None

    async def is_active(self, run_id: str) -> bool:
        """Queued or running: more events may still come."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
                )
            ).one_or_none()
        return row is not None and row.status in ("queued", "running")

    async def events_after(self, run_id: str, after_seq: int) -> list[RunEvent]:
        async with self._engine.connect() as connection:
            return await _events(connection, run_id, after_seq=after_seq)

    async def messages(self, run_id: str, *, owner: str) -> list[Message]:
        """The model-facing history of a run's current epoch."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT state->'messages' AS messages FROM runs "
                        "WHERE id = :id AND user_id = :owner"
                    ),
                    {"id": run_id, "owner": owner},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"run {run_id} not found")
        return _MESSAGES.validate_python(row.messages or [])

    async def blob(self, blob_id: str, *, owner: str) -> str:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT b.content FROM tool_result_blobs b JOIN runs r ON r.id = b.run_id "
                        "WHERE b.id = :id AND r.user_id = :owner"
                    ),
                    {"id": blob_id, "owner": owner},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"blob {blob_id} not found")
        return str(row.content)


async def _events(
    connection: AsyncConnection, run_id: str, *, after_seq: int = -1
) -> list[RunEvent]:
    rows = await connection.execute(
        text("SELECT payload FROM run_events WHERE run_id = :run_id AND seq > :after ORDER BY seq"),
        {"run_id": run_id, "after": after_seq},
    )
    payloads: list[Any] = [row.payload for row in rows]
    return _EVENTS.validate_python(payloads)
