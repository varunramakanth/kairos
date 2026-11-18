"""Engine construction."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    """Create the async engine. ``database_url`` must use the ``postgresql+asyncpg`` driver."""
    return create_async_engine(database_url, pool_pre_ping=True)
