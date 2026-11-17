"""Builds concrete objects from settings. The only place that knows every package."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import Lease, PostgresCheckpointer
from kairos_harness import AgentLoop, Checkpointer, Hooks, Provider
from kairos_providers import anthropic_provider, bedrock_provider
from kairos_tools import ToolRegistry
from kairos_tools.builtin import current_time_tool
from kairos_worker.settings import Settings


def build_provider(settings: Settings) -> Provider:
    if settings.provider == "bedrock":
        return bedrock_provider(
            region=settings.bedrock_region, model_prefix=settings.bedrock_model_prefix
        )
    return anthropic_provider()


def build_tools() -> ToolRegistry:
    return ToolRegistry([current_time_tool(lambda: datetime.now(UTC))])


def build_loop(
    provider: Provider, checkpointer: Checkpointer, hooks: Hooks | None = None
) -> AgentLoop:
    return AgentLoop(provider=provider, tools=build_tools(), checkpointer=checkpointer, hooks=hooks)


def build_durable_loop(
    settings: Settings,
    engine: AsyncEngine,
    provider: Provider,
    lease: Lease,
    hooks: Hooks | None = None,
) -> AgentLoop:
    checkpointer = PostgresCheckpointer(engine, lease, lease_ttl_seconds=settings.lease_ttl_seconds)
    return build_loop(provider, checkpointer, hooks)
