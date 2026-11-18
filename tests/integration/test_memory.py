"""Memory on Postgres: keyword recall, proposals, confirmation and the API."""

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
