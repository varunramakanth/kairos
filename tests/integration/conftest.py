"""Fixtures for tests that need a real Postgres (testcontainers).

The container is started once per session and migrated with Alembic, so the
migration itself is under test. Each test gets empty tables.
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from kairos_db import create_engine
from kairos_db.migrate import upgrade

IMAGE = "pgvector/pgvector:pg17"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    try:
        container = PostgresContainer(IMAGE, driver="asyncpg")
        container.start()
    except Exception as error:
        if os.environ.get("KAIROS_REQUIRE_DOCKER") == "1":
            raise
        pytest.skip(f"Docker is not available for integration tests: {error}")
    try:
        upgrade(container.get_connection_url())
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def database_url(postgres: PostgresContainer) -> str:
    return postgres.get_connection_url()


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE threads, runs, jobs, notifications, audit_log, "
                "push_subscriptions, app_settings, eval_runs, eval_labels, memories CASCADE"
            )
        )
        # Users (ADR-018): back to the migrated state, the unbound ``local`` admin.
        await connection.execute(text("DELETE FROM users WHERE id <> 'local'"))
        await connection.execute(
            text(
                "INSERT INTO users (id, email, role, email_verified) "
                "VALUES ('local', NULL, 'admin', true) "
                "ON CONFLICT (id) DO UPDATE SET email = NULL, daily_cap_usd = NULL, "
                "disabled = false"
            )
        )
    yield engine
    await engine.dispose()
