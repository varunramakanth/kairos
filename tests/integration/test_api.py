"""The HTTP API end to end: FastAPI, the embedded worker and Postgres, with a scripted
model. Covers threads, multi-turn history, the SSE stream and its resume rule."""

import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_api.app import create_app
from kairos_api.settings import ApiSettings
from kairos_db import JobStore
from kairos_domain.content import TextBlock
from kairos_domain.jobs import JobSpec, JobStatus
from kairos_harness.testing import ScriptedProvider, Turn, text_turn

pytestmark = pytest.mark.integration


@asynccontextmanager
async def api(
    database_url: str, turns: Sequence[Turn] = (), *, worker: bool = True
) -> AsyncGenerator[tuple[httpx2.AsyncClient, ScriptedProvider]]:
    provider = ScriptedProvider(turns)
    settings = ApiSettings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings init option, not a field
        database_url=database_url,
        embedded_worker=worker,
        poll_seconds=0.05,
        model="claude-haiku-5-5",
        # Pinned off: `just` loads the developer's .env, and tests must never use it.
        email_provider=None,
        email_to=None,
        vapid_public_key=None,
        vapid_private_key=None,
        memory_embeddings="none",  # keyword recall; tests never load a model
        backups=False,
    )
    app: FastAPI = create_app(settings, provider_factory=lambda: provider)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://kairos.test") as client:
            yield client, provider


def sse_events(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into event payloads, adding the frame id as ``_id``."""
    events: list[dict[str, Any]] = []
    for frame in body.split("\n\n"):
        data = [line[6:] for line in frame.splitlines() if line.startswith("data: ")]
        ids = [line[4:] for line in frame.splitlines() if line.startswith("id: ")]
        if data:
            payload = json.loads("\n".join(data))
            payload["_id"] = ids[0] if ids else None
            events.append(payload)
    return events


async def stream(client: httpx2.AsyncClient, run_id: str, **headers: str) -> list[dict[str, Any]]:
    response = await client.get(f"/runs/{run_id}/events", headers=headers, timeout=10)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return sse_events(response.text)


async def test_a_thread_streams_its_runs_and_keeps_history(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url, [text_turn("Hello!"), text_turn("You said hi.")]) as (
        client,
        provider,
    ):
        created = await client.post("/threads", json={"text": "hi"})
        assert created.status_code == 201
        thread_id, first_run = created.json()["thread_id"], created.json()["run_id"]

        events = await stream(client, first_run)
        types = [e["type"] for e in events]
        assert types[0] == "run_started"
        assert types[-1] == "run_finished"
        assert events[-1]["output_text"] == "Hello!"
        durable = [e for e in events if e["seq"] is not None]
        assert [e["_id"] for e in durable] == [str(e["seq"]) for e in durable]

        second = await client.post(f"/threads/{thread_id}/messages", json={"text": "what?"})
        assert second.status_code == 202
        events = await stream(client, second.json()["run_id"])
        assert events[-1]["output_text"] == "You said hi."

        # The second run saw the first turn, unchanged, before the new input.
        sent = provider.requests[1].messages
        assert [m.role for m in sent] == ["user", "assistant", "user"]
        assert sent[1].content == (TextBlock(text="Hello!"),)
        assert sent[2].text == "what?"
        assert provider.requests[1].model == "claude-haiku-5-5"

        thread = (await client.get(f"/threads/{thread_id}")).json()
        assert thread["thread"]["title"] == "hi"
        assert [r["run"]["input"] for r in thread["runs"]] == ["hi", "what?"]
        assert all(r["run"]["status"] == "finished" for r in thread["runs"])
        assert thread["runs"][0]["events"][-1]["type"] == "run_finished"

        listed = (await client.get("/threads")).json()
        assert [t["id"] for t in listed] == [thread_id]

        messages = (await client.get(f"/runs/{second.json()['run_id']}/messages")).json()
        assert len(messages) == 4


async def test_reconnecting_with_last_event_id_replays_only_what_was_missed(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url, [text_turn("Hello!")]) as (client, _):
        run_id = (await client.post("/threads", json={"text": "hi"})).json()["run_id"]
        full = [e for e in await stream(client, run_id) if e["seq"] is not None]

        resumed = await stream(client, run_id, **{"Last-Event-ID": str(full[2]["seq"])})
        assert [e["seq"] for e in resumed] == [e["seq"] for e in full[3:]]

        # A finished run that has nothing new ends the stream at once.
        assert await stream(client, run_id, **{"Last-Event-ID": str(full[-1]["seq"])}) == []


async def test_a_thread_takes_one_turn_at_a_time(engine: AsyncEngine, database_url: str) -> None:
    async with api(database_url, worker=False) as (client, _):
        thread_id = (await client.post("/threads", json={"text": "first"})).json()["thread_id"]
        busy = await client.post(f"/threads/{thread_id}/messages", json={"text": "second"})
        assert busy.status_code == 409


async def test_unknown_ids_are_404(engine: AsyncEngine, database_url: str) -> None:
    async with api(database_url, worker=False) as (client, _):
        assert (await client.get("/threads/thr_nope")).status_code == 404
        assert (await client.get("/runs/run_nope")).status_code == 404
        assert (await client.get("/runs/run_nope/events")).status_code == 404
        assert (await client.get("/blobs/blob_nope")).status_code == 404
        missing = await client.post("/threads/thr_nope/messages", json={"text": "x"})
        assert missing.status_code == 404
        assert (await client.post("/threads", json={"text": ""})).status_code == 422


WATCH_BODY: dict[str, Any] = {
    "name": "Monitor price",
    "kind": "watch",
    "schedule": {"kind": "interval", "every_seconds": 21600},
    "watch": {
        "target": "https://shop.example/monitor",
        "extract": "price in CAD",
        "condition": {"kind": "below", "threshold": 400},
    },
}


async def test_jobs_can_be_created_edited_paused_and_listed(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url, worker=False) as (client, _):
        created = await client.post("/jobs", json=WATCH_BODY)
        assert created.status_code == 201, created.text
        job = created.json()
        assert job["status"] == "active"
        assert job["next_run_at"] is not None

        edited = await client.patch(
            f"/jobs/{job['id']}", json={"spec": {**WATCH_BODY, "name": "Monitor under $400"}}
        )
        assert edited.json()["spec"]["name"] == "Monitor under $400"
        paused = await client.patch(f"/jobs/{job['id']}", json={"status": "paused"})
        assert paused.json()["status"] == "paused"
        assert (await client.post(f"/jobs/{job['id']}/run")).status_code == 409

        resumed = await client.patch(f"/jobs/{job['id']}", json={"status": "active"})
        assert resumed.json()["status"] == "active"
        assert (await client.post(f"/jobs/{job['id']}/run")).status_code == 200

        (summary,) = (await client.get("/jobs")).json()
        assert summary["job"]["id"] == job["id"]
        assert summary["recent_values"] == []
        detail = (await client.get(f"/jobs/{job['id']}")).json()
        assert detail["runs"] == []

        assert (await client.post("/jobs/pause-all")).json() == {"paused": 1}
        deleted = await client.patch(f"/jobs/{job['id']}", json={"status": "deleted"})
        assert deleted.json()["status"] == "deleted"
        assert (await client.get(f"/jobs/{job['id']}")).status_code == 404
        assert (await client.get("/jobs")).json() == []


async def test_an_agent_proposed_job_waits_for_confirmation(
    engine: AsyncEngine, database_url: str
) -> None:
    store = JobStore(engine)
    now = datetime.now(UTC)
    spec = JobSpec.model_validate(WATCH_BODY)
    await store.create(
        "job_p", spec, status=JobStatus.PENDING_CONFIRMATION, next_run_at=None, now=now
    )
    async with api(database_url, worker=False) as (client, _):
        confirmed = await client.post("/jobs/job_p/confirm")
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "active"
        assert confirmed.json()["next_run_at"] is not None
        assert (await client.post("/jobs/job_p/confirm")).status_code == 409


async def test_invalid_jobs_are_rejected(engine: AsyncEngine, database_url: str) -> None:
    async with api(database_url, worker=False) as (client, _):
        bad_cron = {**WATCH_BODY, "schedule": {"kind": "cron", "expression": "every day"}}
        response = await client.post("/jobs", json=bad_cron)
        assert response.status_code == 422
        assert "five-field" in response.json()["detail"]
        no_watch = {**WATCH_BODY, "watch": None}
        assert (await client.post("/jobs", json=no_watch)).status_code == 422
        too_often = {**WATCH_BODY, "schedule": {"kind": "interval", "every_seconds": 10}}
        assert (await client.post("/jobs", json=too_often)).status_code == 422
        assert (await client.get("/jobs/job_nope")).status_code == 404
