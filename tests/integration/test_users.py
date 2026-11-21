"""M10 end to end: invited users with private data (ADR-018).

The API runs with sign-in on (locally signed RS256 tokens) and a fake Cognito/SES
directory. The owner's first sign-in claims the ``local`` admin; the owner invites
Bea; then every route is called as Bea with the owner's ids and must leak nothing
and change nothing.
"""

import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_api.app import create_app
from kairos_api.auth import Authenticator
from kairos_api.settings import ApiSettings
from kairos_harness.testing import ScriptedProvider, Turn, text_turn, tool_turn
from tests.integration.test_api import stream
from tests.integration.test_governance import NOTIFY, eventually, rows

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean(engine: AsyncEngine) -> None:
    """Every test starts from the migrated users table (the ``engine`` fixture resets it)."""


ISSUER = "https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_TEST"
CLIENT = "client-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OWNER_EMAIL = "owner@example.com"
OWNER_SUB = "sub-owner"
BEA_EMAIL = "bea@example.com"
BEA_SUB = "sub-bea"


async def public_key(token: str) -> Any:
    return KEY.public_key()


def token(sub: str) -> str:
    claims = {
        "iss": ISSUER,
        "client_id": CLIENT,
        "token_use": "access",
        "sub": sub,
        "exp": int(time.time()) + 600,
    }
    return jwt.encode(claims, KEY, algorithm="RS256")


class FakeDirectory:
    """Cognito and SES, in memory. ``emails`` maps a ``sub`` to its userInfo email."""

    def __init__(self) -> None:
        self.emails: dict[str, str] = {OWNER_SUB: OWNER_EMAIL}
        self.invited: list[str] = []
        self.verification_requests: list[str] = []
        self.verified: set[str] = set()
        self.enabled: dict[str, bool] = {}
        self.deleted: list[str] = []
        self.forgotten: list[str] = []

    async def userinfo_email(self, access_token: str) -> str | None:
        sub = jwt.decode(access_token, options={"verify_signature": False})["sub"]
        return self.emails.get(sub)

    async def invite(self, email: str) -> str:
        self.invited.append(email)
        sub = BEA_SUB if email == BEA_EMAIL else f"sub-{len(self.invited)}"
        self.emails[sub] = email
        return sub

    async def set_enabled(self, sub: str, *, enabled: bool) -> None:
        self.enabled[sub] = enabled

    async def delete(self, sub: str) -> None:
        self.deleted.append(sub)

    async def request_verification(self, email: str) -> None:
        self.verification_requests.append(email)

    async def is_verified(self, email: str) -> bool:
        return email in self.verified

    async def forget_email(self, email: str) -> None:
        self.forgotten.append(email)


class Clients:
    def __init__(self, http: httpx2.AsyncClient, app: FastAPI, directory: FakeDirectory) -> None:
        self.http = http
        self.app = app
        self.directory = directory

    def headers(self, sub: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(sub)}"}

    async def as_(self, sub: str, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        return await self.http.request(method, path, headers=self.headers(sub), **kwargs)

    async def owner(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        return await self.as_(OWNER_SUB, method, path, **kwargs)

    async def bea(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        return await self.as_(BEA_SUB, method, path, **kwargs)


@asynccontextmanager
async def api(database_url: str, turns: Sequence[Turn] = ()) -> AsyncGenerator[Clients]:
    provider = ScriptedProvider(turns)
    directory = FakeDirectory()
    settings = ApiSettings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings init option
        database_url=database_url,
        embedded_worker=True,
        poll_seconds=0.05,
        scheduler_poll_seconds=0.05,
        auth_issuer=ISSUER,
        auth_client_id=CLIENT,
        auth_domain="https://kairos-test.auth.ca-central-1.amazoncognito.com",
        # The owner's address; email delivery itself stays off in tests.
        email_to=OWNER_EMAIL,
        email_provider=None,
        vapid_public_key=None,
        vapid_private_key=None,
        memory_embeddings="none",
        backups=False,
        rate_limit_per_minute=1000,
    )
    app = create_app(
        settings,
        provider_factory=lambda: provider,
        directory=directory,
        authenticator=Authenticator(issuer=ISSUER, client_id=CLIENT, keys=public_key),
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://kairos.test") as http:
            yield Clients(http, app, directory)


async def invite_bea(clients: Clients, **body: Any) -> dict[str, Any]:
    response = await clients.owner("POST", "/users", json={"email": BEA_EMAIL, **body})
    assert response.status_code == 201, response.text
    return response.json()


# --- identity --------------------------------------------------------------------


async def test_the_owners_first_sign_in_claims_the_existing_data(
    engine: AsyncEngine, database_url: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO memories (id, kind, content, status, source, user_id) "
                "VALUES ('mem_before', 'preference', 'Prefers aisle seats', 'active', 'user', "
                "'local')"
            )
        )
    async with api(database_url) as clients:
        me = (await clients.owner("GET", "/me")).json()
        assert me == {"id": OWNER_SUB, "email": OWNER_EMAIL, "role": "admin", "auth_enabled": True}
        memories = (await clients.owner("GET", "/memories")).json()
        assert [m["id"] for m in memories] == ["mem_before"]
    assert await rows(engine, "SELECT id FROM users WHERE id = 'local'") == []


async def test_a_stranger_with_a_valid_sign_in_is_not_invited(database_url: str) -> None:
    async with api(database_url) as clients:
        clients.directory.emails["sub-mallory"] = "mallory@example.com"
        response = await clients.as_("sub-mallory", "GET", "/threads")
        assert response.status_code == 403
        assert "not invited" in response.json()["detail"]
        # And the owner can still claim their account afterwards.
        assert (await clients.owner("GET", "/me")).json()["role"] == "admin"


async def test_invites_create_a_member_with_a_cap_and_ask_ses_to_verify(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url) as clients:
        bea = await invite_bea(clients, daily_cap_usd=1.5)
        assert bea["id"] == BEA_SUB
        assert bea["role"] == "member"
        assert bea["daily_cap_usd"] == 1.5
        assert bea["email_verified"] is False
        assert clients.directory.invited == [BEA_EMAIL]
        assert clients.directory.verification_requests == [BEA_EMAIL]
        again = await clients.owner("POST", "/users", json={"email": "BEA@example.com"})
        assert again.status_code == 409

        clients.directory.verified.add(BEA_EMAIL)
        listed = (await clients.owner("GET", "/users")).json()
        assert {u["email"]: u["email_verified"] for u in listed} == {
            OWNER_EMAIL: True,
            BEA_EMAIL: True,
        }
        assert (await clients.bea("GET", "/me")).json()["role"] == "member"
        # Bea has her own weekly digest.
        digests = await rows(
            engine, "SELECT user_id FROM jobs WHERE spec->>'kind' = 'digest' ORDER BY user_id"
        )
        assert [d.user_id for d in digests] == [BEA_SUB]


async def test_members_cannot_use_admin_routes(database_url: str) -> None:
    async with api(database_url) as clients:
        await invite_bea(clients)
        for method, path, body in [
            ("GET", "/users", None),
            ("POST", "/users", {"email": "x@example.com"}),
            ("PATCH", f"/users/{OWNER_SUB}", {"disabled": True}),
            ("DELETE", f"/users/{OWNER_SUB}", None),
            ("GET", "/evals/runs", None),
            ("GET", "/evals/review", None),
            ("PUT", "/settings/kill-switch", {"actions_enabled": False}),
        ]:
            response = await clients.bea(method, path, json=body)
            assert response.status_code == 403, (method, path)


async def test_a_disabled_member_is_locked_out_and_deleting_removes_only_their_data(
    engine: AsyncEngine, database_url: str
) -> None:
    async with api(database_url) as clients:
        await invite_bea(clients)
        await clients.bea(
            "POST", "/memories", json={"content": "Bea likes tea", "kind": "preference"}
        )
        await clients.owner(
            "POST", "/memories", json={"content": "Owner likes coffee", "kind": "preference"}
        )
        off = await clients.owner("PATCH", f"/users/{BEA_SUB}", json={"disabled": True})
        assert off.json()["disabled"] is True
        assert clients.directory.enabled == {BEA_SUB: False}
        assert (await clients.bea("GET", "/memories")).status_code == 403
        assert (
            await clients.owner("PATCH", f"/users/{OWNER_SUB}", json={"disabled": True})
        ).status_code == 409

        assert (await clients.owner("DELETE", f"/users/{BEA_SUB}")).status_code == 204
        assert clients.directory.deleted == [BEA_SUB]
        assert clients.directory.forgotten == [BEA_EMAIL]
        contents = [r.content for r in await rows(engine, "SELECT content FROM memories")]
        assert contents == ["Owner likes coffee"]
        assert (await clients.owner("DELETE", f"/users/{OWNER_SUB}")).status_code == 409


# --- spend ------------------------------------------------------------------------


async def test_each_user_has_their_own_cap(engine: AsyncEngine, database_url: str) -> None:
    async with api(database_url, [text_turn("hi")]) as clients:
        await invite_bea(clients, daily_cap_usd=0)
        refused = await clients.bea("POST", "/threads", json={"text": "hello"})
        assert refused.status_code == 429
        assert "your daily spend cap" in refused.json()["detail"]
        assert (await clients.owner("POST", "/threads", json={"text": "hello"})).status_code == 201


# --- isolation across every route -------------------------------------------------

BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/threads/{thread_id}/messages"): {"text": "what did they say?"},
    ("PATCH", "/jobs/{job_id}"): {"status": "deleted"},
    ("POST", "/approvals/{approval_id}/decide"): {"approve": True},
    ("PATCH", "/memories/{memory_id}"): {"content": "Bea wrote this"},
    ("PATCH", "/users/{user_id}"): {"disabled": True},
    ("POST", "/threads"): {"text": "hello"},
    ("POST", "/jobs"): {
        "name": "Bea's task",
        "kind": "task",
        "prompt": "say hi",
        "schedule": {"kind": "cron", "expression": "0 9 * * *", "timezone": "America/Toronto"},
    },
    ("POST", "/memories"): {"content": "Bea likes tea", "kind": "preference"},
    ("POST", "/users"): {"email": "carl@example.com"},
    ("POST", "/evals/review"): {
        "run_id": "x",
        "example_id": "x",
        "metric_id": "x",
        "human_passed": True,
    },
    ("PUT", "/settings/kill-switch"): {"actions_enabled": False},
    ("POST", "/push/subscriptions"): {
        "endpoint": "https://push.example/bea",
        "keys": {"p256dh": "k", "auth": "a"},
    },
    ("POST", "/push/unsubscribe"): {"endpoint": "https://push.example/owner"},
    ("POST", "/notifications/test"): {},
}
"""A valid body for every route that takes one, so ownership (not validation) is what
refuses Bea."""


async def owners_world(clients: Clients, engine: AsyncEngine) -> dict[str, str]:
    """The owner's thread, run (paused on an approval), blob, job, memory and push."""
    created = (await clients.owner("POST", "/threads", json={"text": "Email me a summary"})).json()
    thread_id, run_id = created["thread_id"], created["run_id"]
    events = await stream(clients.http, run_id, **clients.headers(OWNER_SUB))
    assert events[-1]["stop_reason"] == "awaiting_approval"
    (approval,) = (await clients.owner("GET", "/approvals")).json()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tool_result_blobs (id, run_id, content) "
                "VALUES ('blob_owner', :run, 'OWNER-SECRET-BLOB')"
            ),
            {"run": run_id},
        )
    job = (await clients.owner("POST", "/jobs", json=BODIES[("POST", "/jobs")])).json()
    memory = (
        await clients.owner(
            "POST", "/memories", json={"content": "OWNER-SECRET-MEMORY", "kind": "profile"}
        )
    ).json()
    await clients.owner(
        "POST",
        "/push/subscriptions",
        json={"endpoint": "https://push.example/owner", "keys": {"p256dh": "k", "auth": "a"}},
    )
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "blob_id": "blob_owner",
        "approval_id": approval["id"],
        "job_id": job["id"],
        "memory_id": memory["id"],
        "user_id": OWNER_SUB,
        "eval_run_id": "evr_missing",
    }


async def owned(engine: AsyncEngine, sql: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(await connection.execute(text(sql), {"owner": OWNER_SUB}))


async def snapshot(engine: AsyncEngine) -> list[Any]:
    """Everything the owner has, to check that nothing Bea did changed it."""
    return [
        await owned(engine, "SELECT id, title FROM threads WHERE user_id = :owner"),
        await owned(engine, "SELECT id, status FROM jobs WHERE user_id = :owner ORDER BY id"),
        await owned(engine, "SELECT id, content, status FROM memories WHERE user_id = :owner"),
        await owned(engine, "SELECT id, status FROM approvals"),
        await owned(engine, "SELECT endpoint FROM push_subscriptions WHERE user_id = :owner"),
        await owned(engine, "SELECT count(*) FROM runs WHERE user_id = :owner"),
        await owned(engine, "SELECT disabled FROM users WHERE id = :owner"),
    ]


def routes(app: FastAPI) -> list[tuple[str, str, bool]]:
    """``(method, path, takes_a_body)`` for every API route."""
    found: list[tuple[str, str, bool]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            takes_body = route.body_field is not None
            found.extend((method, route.path, takes_body) for method in sorted(route.methods or ()))
    return found


async def test_no_route_shows_or_changes_another_users_data(
    engine: AsyncEngine, database_url: str
) -> None:
    turns = [tool_turn(NOTIFY), *[text_turn("ok") for _ in range(10)]]
    async with api(database_url, turns) as clients:
        ids = await owners_world(clients, engine)
        await invite_bea(clients)
        before = await snapshot(engine)
        hidden = set(ids.values()) - {"evr_missing", OWNER_SUB}
        secrets = [*hidden, "OWNER-SECRET", OWNER_EMAIL]

        checked: list[tuple[str, str]] = []
        for method, path, takes_body in routes(clients.app):
            if path in {"/health", "/auth/config"}:
                continue
            params = {name.strip("{}") for name in path.split("/") if name.startswith("{")}
            missing = params - ids.keys()
            assert not missing, f"add an owner id for {missing} ({method} {path})"
            body = BODIES.get((method, path))
            if takes_body:
                assert body is not None, f"add a valid body for {method} {path} to BODIES"
            url = path.format(**ids)
            response = await clients.bea(method, url, json=body)
            assert response.status_code < 500, (method, path, response.text)
            # A 404 may echo the id Bea sent; anything else of the owner's is a leak.
            leaked = [s for s in secrets if s in response.text and s not in url]
            assert not leaked, f"{method} {path} showed Bea {leaked}"
            if params:
                assert response.status_code in {403, 404}, (method, path, response.status_code)
            checked.append((method, path))

        assert len(checked) >= 40  # every route was walked
        await eventually(lambda: _quiet(engine))
        assert await snapshot(engine) == before


async def _quiet(engine: AsyncEngine) -> bool:
    busy = await rows(engine, "SELECT 1 FROM runs WHERE status IN ('queued', 'running')")
    return not busy


async def test_memory_search_and_duplicates_are_per_user(engine: AsyncEngine) -> None:
    from kairos_db.memories import MemoryStore
    from kairos_db.users import UserStore
    from kairos_domain.memory import MemoryKind, MemorySource, MemoryStatus
    from kairos_domain.users import Role

    await UserStore(engine).create(BEA_SUB, BEA_EMAIL, role=Role.MEMBER, daily_cap_usd=None)
    store = MemoryStore(engine)
    mine = await store.create(
        "Allergic to peanuts",
        MemoryKind.PROFILE,
        owner="local",
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
    )
    # The same fact is not a "duplicate" for someone else.
    theirs = await store.propose(
        "Allergic to peanuts",
        MemoryKind.PROFILE,
        owner=BEA_SUB,
        source=MemorySource.AGENT,
        run_id=None,
    )
    assert theirs is not None
    found = await store.search("peanuts", owner=BEA_SUB)
    assert found == []  # Bea's copy is only proposed; the owner's is not hers
    assert [m.id for m in await store.search("peanuts", owner="local")] == [mine.id]
    assert await store.get(mine.id, owner=BEA_SUB) is None
    assert await store.delete(mine.id, owner=BEA_SUB) is False


async def test_a_members_scheduled_job_runs_as_them(engine: AsyncEngine, database_url: str) -> None:
    async with api(database_url, [text_turn("Done.")]) as clients:
        await invite_bea(clients)
        job = (await clients.bea("POST", "/jobs", json=BODIES[("POST", "/jobs")])).json()
        assert (await clients.bea("POST", f"/jobs/{job['id']}/run")).status_code == 200

        async def fired() -> bool:
            return bool(
                await rows(
                    engine, "SELECT 1 FROM runs WHERE request->'metadata'->>'job_id' IS NOT NULL"
                )
            )

        await eventually(fired)
        (run,) = await rows(
            engine, "SELECT user_id FROM runs WHERE request->'metadata'->>'job_id' IS NOT NULL"
        )
        assert run.user_id == BEA_SUB
        assert (await clients.owner("GET", f"/jobs/{job['id']}")).status_code == 404
