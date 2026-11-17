"""Builds concrete objects from settings. The only place that knows every package."""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import Lease, PostgresCheckpointer
from kairos_harness import AgentLoop, Checkpointer, Hooks, LoopConfig, Provider, ToolExecutor
from kairos_harness.cassette import ReplayProvider
from kairos_providers import anthropic_provider, bedrock_provider
from kairos_tools import ToolRegistry
from kairos_tools.builtin import current_time_tool
from kairos_tools.plan import plan_tool
from kairos_tools.web import web_fetch_tool, web_search_tool
from kairos_worker.settings import Settings

log = structlog.get_logger(__name__)


def build_provider(settings: Settings) -> Provider:
    if settings.provider == "replay":
        if settings.replay_cassette is None:
            raise ValueError("KAIROS_PROVIDER=replay needs KAIROS_REPLAY_CASSETTE")
        return ReplayProvider(settings.replay_cassette, name="anthropic")
    if settings.provider == "bedrock":
        return bedrock_provider(
            region=settings.bedrock_region, model_prefix=settings.bedrock_model_prefix
        )
    return anthropic_provider()


def http_client() -> httpx2.AsyncClient:
    """The client for HTTP tools. Redirects are followed by ``web_fetch`` itself, so it
    can check each hop."""
    return httpx2.AsyncClient(follow_redirects=False)


def build_tools(settings: Settings, http: httpx2.AsyncClient) -> ToolRegistry:
    """The tool set, in a fixed order (the order is part of the cached prompt prefix)."""
    tools = ToolRegistry([current_time_tool(lambda: datetime.now(UTC)), plan_tool()])
    if settings.tavily_api_key is not None:
        tools.register(web_search_tool(settings.tavily_api_key, http))
    else:
        log.warning("web_search_disabled", reason="TAVILY_API_KEY is not set")
    tools.register(web_fetch_tool(http))
    return tools


@dataclass(frozen=True)
class Runtime:
    """Everything a run needs that outlives a single run."""

    settings: Settings
    provider: Provider
    tools: ToolExecutor
    hooks: Hooks | None = None

    def loop(self, checkpointer: Checkpointer) -> AgentLoop:
        config = LoopConfig(compact_at_prompt_tokens=self.settings.compact_at_prompt_tokens)
        return AgentLoop(
            provider=self.provider,
            tools=self.tools,
            checkpointer=checkpointer,
            hooks=self.hooks,
            config=config,
        )

    def durable_loop(self, engine: AsyncEngine, lease: Lease) -> AgentLoop:
        checkpointer = PostgresCheckpointer(
            engine, lease, lease_ttl_seconds=self.settings.lease_ttl_seconds
        )
        return self.loop(checkpointer)
