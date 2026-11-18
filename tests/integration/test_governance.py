"""M4 end to end: strict approvals, the kill switch, audit log and notification outbox.

API + embedded worker + Postgres with a scripted model. Notifications go to the
console channel only (no email or push is configured in tests).
"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_api.app import create_app
from kairos_api.settings import ApiSettings
from kairos_harness.testing import ScriptedProvider, Turn, text_turn, tool_turn
from tests.integration.test_api import stream

pytestmark = pytest.mark.integration

NOTIFY_INPUT: dict[str, JsonValue] = {"title": "Daily summary", "body": "All quiet."}
NOTIFY: tuple[str, str, dict[str, JsonValue]] = ("t_notify", "notify_me", NOTIFY_INPUT)


@asynccontextmanager
async def api(
    database_url: str, turns: Sequence[Turn]
) -> AsyncGenerator[tuple[httpx2.AsyncClient, ScriptedProvider]]:
    provider = ScriptedProvider(turns)
    settings = ApiSettings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings init option
        database_url=database_url,
        embedded_worker=True,
        poll_seconds=0.05,
        scheduler_poll_seconds=0.05,
        # Pinned off: `just` loads the developer's .env into the environment, and these
        # tests must never see (or send through) a real email or push setup.
        email_provider=None,
        email_to=None,
        vapid_public_key=None,
        vapid_private_key=None,
        memory_embeddings="none",  # keyword recall; tests never load a model
    )
    app = create_app(settings, provider_factory=lambda: provider)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://kairos.test") as client:
            yield client, provider


async def rows(engine: AsyncEngine, sql: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(await connection.execute(text(sql)))


async def eventually(check: Any, within: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def test_a_side_effect_waits_for_approval_then_runs_once(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url, [tool_turn(NOTIFY), text_turn("Sent you the summary.")]) as (
        client,
        _,
    ):
        run_id = (await client.post("/threads", json={"text": "Email me a summary"})).json()[
            "run_id"
        ]
        events = await stream(client, run_id)
        assert events[-1]["stop_reason"] == "awaiting_approval"
        asked = next(e for e in events if e["type"] == "approval_requested")
        assert asked["risk"] == "write_external"
        assert asked["input"] == NOTIFY[2]

        (pending,) = (await client.get("/approvals", params={"status": "pending"})).json()
        assert pending["name"] == "notify_me"
        assert (await client.get(f"/runs/{run_id}/approvals")).json()[0]["id"] == pending["id"]
        # Nothing was sent yet: only the "approval needed" notice is queued.
        assert [r.kind for r in await rows(engine, "SELECT kind FROM notifications")] == [
            "approval"
        ]

        decided = await client.post(f"/approvals/{pending['id']}/decide", json={"approve": True})
        assert decided.json()["status"] == "approved"
        again = await client.post(f"/approvals/{pending['id']}/decide", json={"approve": True})
        assert again.status_code == 409

        events = await stream(client, run_id, **{"Last-Event-ID": str(events[-1]["seq"])})
        assert events[-1]["stop_reason"] == "completed"
        assert events[-1]["output_text"] == "Sent you the summary."
        result = next(e for e in events if e["type"] == "tool_call_result")
        assert result["output"].startswith(
            "Recorded, but NOT delivered"
        )  # no email or push in tests

        async def all_sent() -> bool:
            statuses = await rows(engine, "SELECT kind, status FROM notifications ORDER BY id")
            return [(r.kind, r.status) for r in statuses] == [
                ("approval", "sent"),
                ("message", "sent"),
            ]

        await eventually(all_sent)
        (message,) = await rows(
            engine, "SELECT payload, results FROM notifications WHERE kind = 'message'"
        )
        assert message.payload["channels"] == ["console"]  # only what is configured
        assert message.results == {"console": "sent"}
        actions = [r.action for r in await rows(engine, "SELECT action FROM audit_log ORDER BY id")]
        for expected in (
            "approval.requested",
            "approval.approved",
            "tool.executed",
            "notification.sent",
        ):
            assert expected in actions
        audit = (await client.get("/audit")).json()
        assert audit[0]["id"] > audit[-1]["id"]  # newest first


async def test_a_declined_action_never_runs_and_the_model_is_told(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url, [tool_turn(NOTIFY), text_turn("OK, I won't send it.")]) as (
        client,
        _,
    ):
        run_id = (await client.post("/threads", json={"text": "Email me"})).json()["run_id"]
        events = await stream(client, run_id)
        (pending,) = (await client.get("/approvals", params={"status": "pending"})).json()
        await client.post(
            f"/approvals/{pending['id']}/decide", json={"approve": False, "note": "not now"}
        )

        events = await stream(client, run_id, **{"Last-Event-ID": str(events[-1]["seq"])})
        result = next(e for e in events if e["type"] == "tool_call_result")
        assert result["is_error"]
        assert result["output"] == "The user declined this action. Note: not now"
        assert events[-1]["stop_reason"] == "completed"
        kinds = [r.kind for r in await rows(engine, "SELECT kind FROM notifications")]
        assert "message" not in kinds


async def test_the_kill_switch_denies_actions_and_pauses_jobs(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url, [tool_turn(NOTIFY), text_turn("Actions are switched off.")]) as (
        client,
        _,
    ):

        async def digest_created() -> bool:  # the worker creates it as it starts
            jobs = (await client.get("/jobs")).json()
            return [j["job"]["id"] for j in jobs] == ["job_weekly_digest"]

        await eventually(digest_created)

        settings = (
            await client.put("/settings/kill-switch", json={"actions_enabled": False})
        ).json()
        assert settings["actions_enabled"] is False
        assert (await client.get("/jobs")).json()[0]["job"]["status"] == "paused"

        run_id = (await client.post("/threads", json={"text": "Email me"})).json()["run_id"]
        events = await stream(client, run_id)
        result = next(e for e in events if e["type"] == "tool_call_result")
        assert result["output"] == "not allowed: agent actions are switched off (kill switch)"
        assert (await client.get("/approvals")).json() == []
        actions = [r.action for r in await rows(engine, "SELECT action FROM audit_log")]
        assert "kill_switch.on" in actions


async def test_settings_and_test_notifications(engine: AsyncEngine, database_url: str) -> None:
    async with api(database_url, []) as (client, _):
        settings = (await client.get("/settings")).json()
        assert settings == {
            "actions_enabled": True,
            "email_to": None,
            "email_configured": False,
            "push_configured": False,
            "vapid_public_key": None,
            "timezone": "America/Toronto",
            "spent_today_usd": 0.0,
            "daily_spend_cap_usd": 5.0,
        }
        queued = await client.post("/notifications/test", json={})
        assert queued.json() == {"channels": ["console", "push"]}

        async def done() -> bool:
            sent = (await client.get("/notifications")).json()
            return bool(sent) and sent[0]["status"] == "sent"

        await eventually(done)
        (test,) = (await client.get("/notifications")).json()
        assert test["results"] == {"console": "sent", "push": "skipped (not configured)"}

        subscription = {
            "endpoint": "https://push.example/abc",
            "keys": {"p256dh": "k", "auth": "a"},
        }
        assert (await client.post("/push/subscriptions", json=subscription)).status_code == 201
        assert (
            await client.post("/push/unsubscribe", json={"endpoint": subscription["endpoint"]})
        ).status_code == 200
        bad = {"endpoint": "http://insecure.example", "keys": {"p256dh": "k", "auth": "a"}}
        assert (await client.post("/push/subscriptions", json=bad)).status_code == 422
