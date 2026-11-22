"""The daily spend cap and API rate limits (ADR-015), on Postgres."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_api.app import create_app
from kairos_api.limits import RateLimiter
from kairos_api.settings import ApiSettings
from kairos_db.governance import NotificationStore
from kairos_db.spend import SpendLedger
from kairos_domain.model import ModelRequest
from kairos_domain.run import RunRequest, RunState
from kairos_domain.users import LOCAL_USER
from kairos_harness import BudgetStop, FakeClock
from kairos_harness.testing import ScriptedProvider, text_turn
from kairos_worker.spend import SpendCap, SpendCapHooks
from tests.integration.test_governance import rows

pytestmark = pytest.mark.integration

NOON = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)  # 12:00 in Toronto


async def spend(
    engine: AsyncEngine, amount: float, *, at: datetime = NOON, owner: str = LOCAL_USER
) -> None:
    """A finished run of ``owner`` whose one model call cost ``amount``."""
    run_id = f"run_{abs(hash((amount, at, owner))) % 10**9}"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO runs (id, status, request, user_id) VALUES (:id, 'finished', "
                "CAST(:request AS jsonb), :owner)"
            ),
            {
                "id": run_id,
                "owner": owner,
                "request": RunRequest(run_id=run_id, input="x", model="m").model_dump_json(),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO run_steps (run_id, step, model, stop_reason, input_tokens, "
                "output_tokens, cache_read_input_tokens, cache_creation_input_tokens, cost_usd, "
                "created_at) VALUES (:id, 1, 'm', 'end_turn', 0, 0, 0, 0, :cost, :at)"
            ),
            {"id": run_id, "cost": amount, "at": at},
        )


async def test_spend_counts_the_local_day_only(engine: AsyncEngine) -> None:
    ledger = SpendLedger(engine, timezone="America/Toronto")
    await spend(engine, 1.25)
    await spend(engine, 2.00, at=datetime(2026, 11, 18, 4, 30, tzinfo=UTC))  # 23:30 yesterday
    await spend(engine, 0.75, at=datetime(2026, 11, 18, 6, 0, tzinfo=UTC))  # 01:00 today
    assert await ledger.spent_today(NOON) == pytest.approx(2.00)  # pyright: ignore[reportUnknownMemberType]
    assert ledger.resets_at(NOON).isoformat() == "2026-11-19T00:00:00-05:00"


async def test_the_cap_stops_runs_mid_way_and_notifies_once_a_day(engine: AsyncEngine) -> None:
    clock = FakeClock(NOON)
    cap = SpendCap(SpendLedger(engine, timezone="America/Toronto"), cap_usd=5.0, clock=clock)
    hooks = SpendCapHooks(cap, NotificationStore(engine), clock=clock)
    state = RunState(request=RunRequest(run_id="r1", input="x", model="m"))
    request = ModelRequest(model="m", system="s", messages=())

    await spend(engine, 4.99)
    assert await hooks.before_model(state, request) is request  # under the cap
    await spend(engine, 0.02)
    for _ in range(2):
        with pytest.raises(BudgetStop, match=r"daily spend cap of \$5.00 reached \(\$5.01"):
            await hooks.before_model(state, request)
    notices: list[Any] = await rows(engine, "SELECT kind, payload FROM notifications")
    assert len(notices) == 1  # deduplicated per day
    assert "spend cap" in notices[0].payload["title"]


@asynccontextmanager
async def api(database_url: str, **overrides: Any) -> AsyncGenerator[httpx2.AsyncClient]:
    settings = ApiSettings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings init option
        database_url=database_url,
        embedded_worker=False,
        email_provider=None,
        memory_embeddings="none",
        backups=False,
        **overrides,
    )
    app = create_app(settings, provider_factory=lambda: ScriptedProvider([text_turn("hi")]))
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://kairos.test") as client:
            yield client


async def test_the_api_refuses_new_runs_over_the_cap_and_reports_spend(
    engine: AsyncEngine, database_url: str
) -> None:
    await spend(engine, 3.10, at=datetime.now(UTC))
    async with api(database_url, daily_spend_cap_usd=3.0) as client:
        refused = await client.post("/threads", json={"text": "Hello"})
        assert refused.status_code == 429
        assert refused.json()["detail"].startswith("daily spend cap of $3.00 reached")
        settings = (await client.get("/settings")).json()
        assert settings["spent_today_usd"] == pytest.approx(3.10)  # pyright: ignore[reportUnknownMemberType]
        assert settings["daily_spend_cap_usd"] == 3.0


async def test_work_starting_endpoints_are_rate_limited(database_url: str) -> None:
    async with api(database_url, rate_limit_per_minute=2) as client:
        codes = [(await client.post("/notifications/test", json={})).status_code for _ in range(3)]
        assert codes == [202, 202, 429]
        last = await client.post("/notifications/test", json={})
        assert int(last.headers["retry-after"]) >= 1


def test_the_rate_limiter_window_slides() -> None:
    now = [0.0]
    limiter = RateLimiter(limit=2, per_seconds=60, now=lambda: now[0])
    assert [limiter.check("a"), limiter.check("a")] == [None, None]
    assert limiter.check("a") == 60
    assert limiter.check("b") is None  # per client
    now[0] = 61
    assert limiter.check("a") is None
