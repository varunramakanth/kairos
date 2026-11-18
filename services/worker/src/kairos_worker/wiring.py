"""Builds concrete objects from settings. The only place that knows every package."""

import sys
from dataclasses import dataclass

import httpx2
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import Lease, PostgresCheckpointer
from kairos_domain.tools import ToolRisk
from kairos_harness import (
    AgentLoop,
    Checkpointer,
    Clock,
    Hooks,
    LoopConfig,
    Provider,
    SystemClock,
    ToolExecutor,
)
from kairos_harness.cassette import ReplayProvider
from kairos_notify.tools import Enqueue, notify_tool
from kairos_providers import anthropic_provider, bedrock_provider
from kairos_scheduler.protocols import JobRepository
from kairos_scheduler.tools import record_observation_tool, schedule_tools
from kairos_tools import ToolRegistry
from kairos_tools.builtin import current_time_tool
from kairos_tools.mcp import McpServerConfig, McpToolPolicy
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


def _browser_policies() -> dict[str, McpToolPolicy]:
    read = McpToolPolicy(risk=ToolRisk.READ, idempotent=True)
    act = McpToolPolicy(risk=ToolRisk.WRITE_EXTERNAL)
    return {
        **dict.fromkeys(("navigate", "snapshot", "extract", "click", "search"), read),
        **dict.fromkeys(("type", "press"), act),
    }


BROWSER_SERVER = McpServerConfig(
    name="browser",
    command=sys.executable,
    args=["-m", "kairos_browser.server"],
    tools=_browser_policies(),
    timeout_seconds=45.0,
)
"""The browser service (ADR-012): reading is allowed, typing and pressing need approval,
and every page is untrusted content."""


def mcp_configs(settings: Settings) -> list[McpServerConfig]:
    """MCP servers to connect, in a fixed order (tool order is part of the prompt cache)."""
    return [*([BROWSER_SERVER] if settings.browser else []), *settings.mcp_servers]


def build_tools(
    settings: Settings,
    http: httpx2.AsyncClient,
    *,
    jobs: JobRepository | None = None,
    notify: Enqueue | None = None,
    clock: Clock | None = None,
) -> ToolRegistry:
    """The tool set, in a fixed order (the order is part of the cached prompt prefix).

    The schedule tools and ``notify_me`` need the database, so they are only offered
    when ``jobs`` and ``notify`` are given (not in the database-free ``kairos run``).
    """
    clock = clock or SystemClock()
    tools = ToolRegistry([current_time_tool(clock.now), plan_tool()])
    if settings.tavily_api_key is not None:
        tools.register(web_search_tool(settings.tavily_api_key, http))
    else:
        log.warning("web_search_disabled", reason="TAVILY_API_KEY is not set")
    tools.register(web_fetch_tool(http))
    tools.register(record_observation_tool())
    if jobs is not None:
        for item in schedule_tools(jobs, clock, timezone=settings.timezone):
            tools.register(item)
    if notify is not None:
        tools.register(notify_tool(notify, settings.channels))
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
