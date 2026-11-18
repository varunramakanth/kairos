"""Memory on Postgres: hybrid recall, proposals, confirmation and the API."""

from collections.abc import Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db.memories import MemoryStore
from kairos_domain.memory import MemoryKind, MemorySource, MemoryStatus, MemoryUpdate
from tests.integration.test_governance import api, rows

pytestmark = pytest.mark.integration


async def test_recall_finds_confirmed_memories_by_keyword_and_recency(engine: AsyncEngine) -> None:
    store = MemoryStore(engine)
    aisle = await store.create(
        "Prefers aisle seats on long flights",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    old = await store.create(
        "Prefers window seats on short flights",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    async with engine.begin() as connection:  # age one memory by a year
        await connection.execute(
            text("UPDATE memories SET updated_at = now() - interval '365 days' WHERE id = :id"),
            {"id": old.id},
        )
    proposal = await store.propose(
        "Is flying to Lisbon in March", MemoryKind.TASK, source=MemorySource.AGENT, run_id=None
    )
    assert proposal is not None

    found = await store.search("seats flights")
    assert [m.id for m in found] == [aisle.id, old.id]  # equal relevance; newer first
    assert await store.search("Lisbon") == []  # proposals are never recalled
    assert [m.id for m in await store.search("aisl")] == [aisle.id]  # substring fallback

    # English stemming: "flight" matches "flights".
    assert aisle.id in [m.id for m in await store.search("flight")]

    confirmed = await store.update(proposal.id, MemoryUpdate(status=MemoryStatus.ACTIVE))
    assert confirmed is not None
    assert [m.id for m in await store.search("Lisbon March")] == [proposal.id]


async def test_proposals_are_deduplicated_ignoring_case_and_spacing(engine: AsyncEngine) -> None:
    store = MemoryStore(engine)
    first = await store.propose(
        "Lives in Toronto", MemoryKind.PROFILE, source=MemorySource.REFLECTION, run_id=None
    )
    assert first is not None
    again = await store.propose(
        "  lives in   TORONTO ", MemoryKind.PROFILE, source=MemorySource.AGENT, run_id=None
    )
    assert again is None


async def test_the_memory_api_creates_confirms_edits_and_deletes(
    engine: AsyncEngine, database_url: str
) -> None:
    store = MemoryStore(engine)
    proposal = await store.propose(
        "Is vegetarian", MemoryKind.PREFERENCE, source=MemorySource.REFLECTION, run_id=None
    )
    assert proposal is not None
    async with api(database_url, []) as (client, _):
        created = await client.post(
            "/memories", json={"kind": "profile", "content": "Lives in Toronto"}
        )
        assert created.status_code == 201
        assert created.json()["status"] == "active"

        proposed = (await client.get("/memories", params={"status": "proposed"})).json()
        assert [m["id"] for m in proposed] == [proposal.id]
        confirmed = await client.patch(f"/memories/{proposal.id}", json={"status": "active"})
        assert confirmed.json()["status"] == "active"
        edited = await client.patch(
            f"/memories/{proposal.id}", json={"content": "Is vegetarian (eats fish)"}
        )
        assert edited.json()["content"] == "Is vegetarian (eats fish)"

        searched = (await client.get("/memories", params={"q": "fish"})).json()
        assert [m["id"] for m in searched] == [proposal.id]

        assert (await client.delete(f"/memories/{proposal.id}")).status_code == 204
        assert (await client.delete(f"/memories/{proposal.id}")).status_code == 404
        assert (await client.patch("/memories/nope", json={"status": "active"})).status_code == 404

    actions = [r.action for r in await rows(engine, "SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["memory.created", "memory.confirmed", "memory.updated", "memory.deleted"]


async def test_the_browser_may_delete_memories_cross_origin(database_url: str) -> None:
    async with api(database_url, []) as (client, _):
        preflight = await client.options(
            "/memories/mem_1",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "DELETE",
            },
        )
    assert preflight.status_code == 200
    assert "DELETE" in preflight.headers["access-control-allow-methods"]


class TopicEmbedder:
    """Hand-made vectors: each text maps to a topic axis, so meaning, not shared words,
    decides similarity. Stands in for a real model in this test."""

    name = "topics-v1"
    TOPICS = ("seat", "food", "weather")

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        hits = {
            "seat": any(w in lowered for w in ("aisle", "sit", "plane", "seat")),
            "food": any(w in lowered for w in ("vegetarian", "eat", "dinner")),
            "weather": "rain" in lowered,
        }
        vector = [1.0 if hits[t] else 0.0 for t in self.TOPICS] + [0.0] * 381
        if not any(vector):
            vector[383] = 1.0  # an unrelated direction
        return vector

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


async def test_semantic_recall_finds_meaning_without_shared_words(engine: AsyncEngine) -> None:
    store = MemoryStore(engine, embedder=TopicEmbedder())
    aisle = await store.create(
        "Prefers aisle seats on long flights",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    await store.create(
        "Is vegetarian", MemoryKind.PREFERENCE, source=MemorySource.USER, status=MemoryStatus.ACTIVE
    )
    # No word in common with the memory; only the vectors match.
    assert [m.id for m in await store.search("where do I like to sit on a plane")] == [aisle.id]
    # Below the similarity threshold, nothing comes back.
    assert await store.search("quarterly tax deadlines") == []


async def test_memories_saved_without_a_model_are_backfilled(engine: AsyncEngine) -> None:
    plain = MemoryStore(engine)
    memory = await plain.create(
        "Prefers aisle seats",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )

    class Broken(TopicEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            raise RuntimeError("model not downloaded")

    broken = MemoryStore(engine, embedder=Broken())
    kept = await broken.create(
        "Is vegetarian",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    assert kept.content == "Is vegetarian"  # stored anyway, without a vector

    store = MemoryStore(engine, embedder=TopicEmbedder())
    assert await store.backfill() == 2
    assert await store.backfill() == 0
    assert [m.id for m in await store.search("where do I sit on a plane")] == [memory.id]


def _model_cached() -> bool:
    from kairos_memory.embeddings import default_cache

    return any(default_cache().glob("*bge-small-en-v1.5*"))


@pytest.mark.skipif(not _model_cached(), reason="local embedding model not downloaded")
async def test_the_local_model_recalls_by_meaning(engine: AsyncEngine) -> None:
    from kairos_memory.embeddings import LocalEmbedder

    store = MemoryStore(engine, embedder=LocalEmbedder())
    seat = await store.create(
        "Prefers aisle seats on long flights",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    await store.create(
        "Is vegetarian and avoids fish",
        MemoryKind.PREFERENCE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    found = await store.search("where do I like to sit on an airplane")
    assert found
    assert found[0].id == seat.id
