"""Builds concrete objects from settings. The only place that knows every package."""

import sys
from dataclasses import dataclass
from functools import cache

import httpx2
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import Lease, PostgresCheckpointer
from kairos_db.memories import MemoryStore
from kairos_domain.tools import ToolRisk
from kairos_harness import (
    AgentLoop,
    Checkpointer,
    Clock,
    ContentScreen,
    Hooks,
    LoopConfig,
    Provider,
    SystemClock,
    ToolExecutor,
)
from kairos_harness.cassette import ReplayProvider
from kairos_harness.subagents import SubagentProfile, Subagents
from kairos_memory.embeddings import Embedder, LocalEmbedder
from kairos_memory.protocols import MemoryRepository
from kairos_memory.tools import memory_tools
from kairos_notify.tools import Enqueue, notify_tool
from kairos_policy.screening import InjectionScreen
from kairos_providers import anthropic_provider, bedrock_provider
from kairos_scheduler.protocols import JobRepository
from kairos_scheduler.tools import record_observation_tool, schedule_tools
from kairos_tools import ToolRegistry
from kairos_tools.builtin import current_time_tool
from kairos_tools.deals import deals_search_tool
from kairos_tools.flights import FlightProvider, SerpApiFlights, flights_search_tool
from kairos_tools.mcp import McpServerConfig, McpToolPolicy
from kairos_tools.plan import plan_tool
from kairos_tools.subagent import delegate_tool
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
    memory: MemoryRepository | None = None,
    flights: FlightProvider | None = None,
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
        tools.register(deals_search_tool(settings.tavily_api_key, http))
    else:
        log.warning("web_search_disabled", reason="TAVILY_API_KEY is not set")
    tools.register(web_fetch_tool(http))
    if flights is None and settings.serpapi_api_key is not None:
        flights = SerpApiFlights(settings.serpapi_api_key, http, clock=clock)
    if flights is not None:
        tools.register(flights_search_tool(flights))
    tools.register(record_observation_tool())
    if jobs is not None:
        for item in schedule_tools(jobs, clock, timezone=settings.timezone):
            tools.register(item)
    if notify is not None:
        tools.register(notify_tool(notify, settings.channels))
    if memory is not None:
        for item in memory_tools(memory):
            tools.register(item)
    return tools


@cache
def shared_embedder(kind: str) -> Embedder | None:
    """One embedding model per process, shared by the API and an embedded worker."""
    return LocalEmbedder() if kind == "local" else None


def memory_store(settings: Settings, engine: AsyncEngine) -> MemoryStore:
    return MemoryStore(engine, embedder=shared_embedder(settings.memory_embeddings))


SUBAGENT_PROFILES = (
    SubagentProfile(
        name="research",
        description=(
            "Researches one question on the web (search, read pages, the browser when it is "
            "enabled) and returns a short cited summary."
        ),
        tools=frozenset(
            {
                "current_time",
                "web_search",
                "web_fetch",
                "flights_search",
                "deals_search",
                "browser_navigate",
                "browser_snapshot",
                "browser_extract",
                "browser_click",
                "browser_search",
            }
        ),
        instructions="Prefer two or three good sources over many weak ones.",
    ),
)


def build_screen(settings: Settings, provider: Provider) -> ContentScreen | None:
    """Injection screening for untrusted tool output (ADR-015)."""
    if settings.injection_screening == "off":
        return None
    classifier = provider if settings.injection_screening == "model" else None
    return InjectionScreen(classifier, model=settings.job_model)


def add_delegate(
    tools: ToolRegistry,
    settings: Settings,
    provider: Provider,
    *,
    hooks: Hooks | None = None,
    clock: Clock | None = None,
    screen: ContentScreen | None = None,
) -> Subagents:
    """Register ``delegate`` last: children use the tools registered so far (read-only
    ones only), and the tool order stays stable for the prompt cache."""
    subagents = Subagents(
        provider=provider,
        tools=tools,
        profiles=SUBAGENT_PROFILES,
        model=settings.subagent_model,
        hooks=hooks,
        clock=clock,
        screen=screen,
    )
    tools.register(delegate_tool(subagents))
    return subagents


@dataclass(frozen=True)
class Runtime:
    """Everything a run needs that outlives a single run."""

    settings: Settings
    provider: Provider
    tools: ToolExecutor
    hooks: Hooks | None = None
    screen: ContentScreen | None = None

    def loop(self, checkpointer: Checkpointer) -> AgentLoop:
        config = LoopConfig(compact_at_prompt_tokens=self.settings.compact_at_prompt_tokens)
        return AgentLoop(
            provider=self.provider,
            tools=self.tools,
            checkpointer=checkpointer,
            hooks=self.hooks,
            config=config,
            screen=self.screen,
        )

    def durable_loop(self, engine: AsyncEngine, lease: Lease) -> AgentLoop:
        checkpointer = PostgresCheckpointer(
            engine, lease, lease_ttl_seconds=self.settings.lease_ttl_seconds
        )
        return self.loop(checkpointer)
