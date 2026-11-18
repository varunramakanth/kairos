"""Alembic environment. The database URL comes from the Config, set by ``kairos_db.migrate``."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from kairos_db.models import Base

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not set")
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


asyncio.run(_run_online())
