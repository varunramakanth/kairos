"""Memories on Postgres: hybrid recall over full text and pgvector (migrations 0006, 0007).

- **Keyword:** ``ts_rank_cd`` over a generated ``tsvector``, queried with
  ``websearch_to_tsquery`` (plain words and quotes).
- **Semantic (with an embedder):** cosine similarity on ``embedding``; only matches at
  or above ``min_similarity`` count, so an unrelated query still finds nothing.
- **Fusion:** reciprocal-rank fusion of the two lists, scaled by recency (a memory
  untouched for 90 days counts half).
- **Fallback:** a substring match when neither finds anything.

Embedding is best effort: if the model is unavailable the memory is stored without a
vector, keyword recall still works, and ``backfill`` embeds it later.
"""

import uuid
from collections.abc import Sequence
from typing import Any, Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus, MemoryUpdate

log = structlog.get_logger(__name__)

_COLUMNS = "id, kind, content, status, source, run_id, created_at, updated_at"
_RECENCY = "(1.0 / (1.0 + EXTRACT(EPOCH FROM (now() - updated_at)) / 86400.0 / 90.0))"
RRF_K = 60
CANDIDATES = 20


class Embedder(Protocol):
    """Structural twin of ``kairos_memory.embeddings.Embedder``."""

    @property
    def name(self) -> str: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


_KEYWORD = (
    f"SELECT {_COLUMNS} FROM memories, "  # noqa: S608  # constant SQL
    "websearch_to_tsquery('english', :q) AS query "
    "WHERE status = 'active' AND search @@ query "
    f"ORDER BY ts_rank_cd(search, query) * {_RECENCY} DESC, updated_at DESC LIMIT :limit"
)

_HYBRID = f"""
WITH query AS (SELECT websearch_to_tsquery('english', :q) AS tsq),
keyword AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(search, query.tsq) DESC) AS rank
    FROM memories, query
    WHERE status = 'active' AND search @@ query.tsq
    ORDER BY rank LIMIT {CANDIDATES}
),
semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:v AS vector)) AS rank
    FROM memories
    WHERE status = 'active' AND embedding IS NOT NULL
      AND embedding <=> CAST(:v AS vector) <= :max_distance
    ORDER BY rank LIMIT {CANDIDATES}
),
fused AS (
    SELECT id, sum(1.0 / ({RRF_K} + rank)) AS score
    FROM (SELECT * FROM keyword UNION ALL SELECT * FROM semantic) AS both_lists
    GROUP BY id
)
SELECT {_COLUMNS} FROM memories JOIN fused USING (id)
ORDER BY fused.score * {_RECENCY} DESC, updated_at DESC
LIMIT :limit
"""  # noqa: S608  # constant SQL; values are bound parameters


def _literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"


def _memory(row: Any) -> Memory:
    return Memory.model_validate(dict(row))


class MemoryStore:
    def __init__(
        self, engine: AsyncEngine, *, embedder: Embedder | None = None, min_similarity: float = 0.55
    ) -> None:
        self._engine = engine
        self._embedder = embedder
        self._min_similarity = min_similarity

    async def _vector(self, content: str) -> str | None:
        if self._embedder is None:
            return None
        try:
            (vector,) = await self._embedder.embed_documents([content])
        except Exception as error:  # a missing model must not lose the memory
            log.warning("memory_embedding_failed", error=str(error))
            return None
        return _literal(vector)

    async def _set_vector(self, connection: Any, memory_id: str, vector: str | None) -> None:
        if vector is None or self._embedder is None:
            return
        await connection.execute(
            text(
                "UPDATE memories SET embedding = CAST(:v AS vector), embedding_model = :m "
                "WHERE id = :id"
            ),
            {"v": vector, "m": self._embedder.name, "id": memory_id},
        )

    async def backfill(self, *, limit: int = 500) -> int:
        """Embed memories stored without a vector (or by another model). Returns how many."""
        if self._embedder is None:
            return 0
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, content FROM memories WHERE embedding IS NULL "
                        "OR embedding_model IS DISTINCT FROM :m LIMIT :limit"
                    ),
                    {"m": self._embedder.name, "limit": limit},
                )
            ).all()
        done = 0
        for row in rows:
            vector = await self._vector(row.content)
            if vector is not None:
                async with self._engine.begin() as connection:
                    await self._set_vector(connection, row.id, vector)
                done += 1
        return done

    async def propose(
        self, content: str, kind: MemoryKind, *, source: MemorySource, run_id: str | None
    ) -> Memory | None:
        vector = await self._vector(content.strip())
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
            memory = _memory(row)
            await self._set_vector(connection, memory.id, vector)
            return memory

    async def create(
        self, content: str, kind: MemoryKind, *, source: MemorySource, status: MemoryStatus
    ) -> Memory:
        vector = await self._vector(content.strip())
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
            memory = _memory(row)
            await self._set_vector(connection, memory.id, vector)
            return memory

    async def search(self, query: str, *, limit: int = 5) -> Sequence[Memory]:
        vector = None
        if self._embedder is not None:
            try:
                vector = _literal(await self._embedder.embed_query(query))
            except Exception as error:
                log.warning("memory_query_embedding_failed", error=str(error))
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(_HYBRID if vector else _KEYWORD),
                        {
                            "q": query,
                            "v": vector,
                            "max_distance": 1.0 - self._min_similarity,
                            "limit": limit,
                        },
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
        vector = await self._vector(change.content.strip()) if change.content else None
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
            if row is None:
                return None
            if change.content:
                await self._set_vector(connection, memory_id, vector)
            return _memory(row)

    async def delete(self, memory_id: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM memories WHERE id = :id"), {"id": memory_id}
            )
            return result.rowcount > 0  # type: ignore[attr-defined]  # CursorResult has rowcount


def _new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:20]}"
