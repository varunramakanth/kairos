"""Memories on Postgres: keyword recall over a generated ``tsvector`` (migration 0006).

Ranking is ``ts_rank_cd`` scaled by recency: a memory updated today counts fully, one
untouched for 90 days counts half. Queries use ``websearch_to_tsquery``, which accepts
plain words and quotes; when nothing matches as words (very short or odd queries),
a substring match is the fallback.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus, MemoryUpdate

_COLUMNS = "id, kind, content, status, source, run_id, created_at, updated_at"
_RECENCY = "(1.0 / (1.0 + EXTRACT(EPOCH FROM (now() - updated_at)) / 86400.0 / 90.0))"


def _memory(row: Any) -> Memory:
    return Memory.model_validate(dict(row))


class MemoryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def propose(
        self, content: str, kind: MemoryKind, *, source: MemorySource, run_id: str | None
    ) -> Memory | None:
        async with self._engine.begin() as connection:
            duplicate = (
                await connection.execute(
                    text(
                        "SELECT 1 FROM memories WHERE "
                        "lower(regexp_replace(content, '\\s+', ' ', 'g')) = "
                        "lower(regexp_replace(:content, '\\s+', ' ', 'g'))"
                    ),
                    {"content": content.strip()},
                )
            ).first()
            if duplicate is not None:
                return None
            row = (
                (
                    await connection.execute(
                        text(
                            "INSERT INTO memories (id, kind, content, status, source, run_id) "  # noqa: S608
                            "VALUES (:id, :kind, :content, 'proposed', :source, :run_id) "
                            f"RETURNING {_COLUMNS}"
                        ),
                        {
                            "id": _new_id(),
                            "kind": kind.value,
                            "content": content.strip(),
                            "source": source.value,
                            "run_id": run_id,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return _memory(row)

    async def create(
        self, content: str, kind: MemoryKind, *, source: MemorySource, status: MemoryStatus
    ) -> Memory:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "INSERT INTO memories (id, kind, content, status, source) "  # noqa: S608
                            "VALUES (:id, :kind, :content, :status, :source) "
                            f"RETURNING {_COLUMNS}"
                        ),
                        {
                            "id": _new_id(),
                            "kind": kind.value,
                            "content": content.strip(),
                            "status": status.value,
                            "source": source.value,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return _memory(row)

    async def search(self, query: str, *, limit: int = 5) -> Sequence[Memory]:
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            f"SELECT {_COLUMNS} FROM memories, "  # noqa: S608  # constant SQL
                            "websearch_to_tsquery('english', :q) AS query "
                            "WHERE status = 'active' AND search @@ query "
                            f"ORDER BY ts_rank_cd(search, query) * {_RECENCY} DESC, "
                            "updated_at DESC "
                            "LIMIT :limit"
                        ),
                        {"q": query, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                rows = (
                    (
                        await connection.execute(
                            text(
                                f"SELECT {_COLUMNS} FROM memories "  # noqa: S608  # constant SQL
                                "WHERE status = 'active' AND content ILIKE :like "
                                "ORDER BY updated_at DESC LIMIT :limit"
                            ),
                            {"like": f"%{query.strip()}%", "limit": limit},
                        )
                    )
                    .mappings()
                    .all()
                )
            return [_memory(r) for r in rows]

    async def recent(
        self, *, status: MemoryStatus | None = None, limit: int = 200
    ) -> Sequence[Memory]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    f"SELECT {_COLUMNS} FROM memories "  # noqa: S608  # constant SQL
                    "WHERE (CAST(:status AS text) IS NULL OR status = :status) "
                    "ORDER BY updated_at DESC LIMIT :limit"
                ),
                {"status": status.value if status else None, "limit": limit},
            )
            return [_memory(r) for r in rows.mappings()]

    async def get(self, memory_id: str) -> Memory | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(f"SELECT {_COLUMNS} FROM memories WHERE id = :id"),  # noqa: S608
                        {"id": memory_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            return _memory(row) if row else None

    async def update(self, memory_id: str, change: MemoryUpdate) -> Memory | None:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "UPDATE memories SET "  # noqa: S608  # constant SQL
                            "kind = COALESCE(CAST(:kind AS text), kind), "
                            "content = COALESCE(CAST(:content AS text), content), "
                            "status = COALESCE(CAST(:status AS text), status), "
                            "updated_at = now() "
                            f"WHERE id = :id RETURNING {_COLUMNS}"
                        ),
                        {
                            "id": memory_id,
                            "kind": change.kind.value if change.kind else None,
                            "content": change.content.strip() if change.content else None,
                            "status": change.status.value if change.status else None,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            return _memory(row) if row else None

    async def delete(self, memory_id: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM memories WHERE id = :id"), {"id": memory_id}
            )
            return result.rowcount > 0  # type: ignore[attr-defined]  # CursorResult has rowcount


def _new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:20]}"
