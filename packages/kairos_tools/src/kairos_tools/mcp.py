"""The generic MCP client: any MCP server's tools become Kairos tools.

An MCP server describes its tools (name, description, JSON input schema) and runs
them. Kairos connects as a client, lists the tools and registers each one, so a new
capability is a configuration entry rather than new code.

Trust is decided here, never by the server. A server's ``readOnlyHint`` and similar
annotations are hints from code we do not control, so they are ignored. Every tool
gets its risk and provenance from ``McpServerConfig``:

- Unlisted tools default to ``write_external`` and ``untrusted``: under the strict
  policy they wait for approval, and whatever they return is wrapped as data.
- ``allow`` limits which of a server's tools are offered at all.
- Names are prefixed (``browser_navigate``) so servers cannot shadow built-in tools
  or each other.
"""

from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any, Self

import structlog
from mcp import Client, StdioServerParameters
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from kairos_domain.content import Provenance
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk, ToolSpec
from kairos_tools.registry import Tool

log = structlog.get_logger(__name__)


class McpToolPolicy(BaseModel):
    """How Kairos treats one MCP tool, whatever the server claims about it."""

    model_config = ConfigDict(frozen=True)

    risk: ToolRisk = ToolRisk.WRITE_EXTERNAL
    provenance: Provenance = Provenance.UNTRUSTED
    idempotent: bool = False


class McpServerConfig(BaseModel):
    """One MCP server, launched as a subprocess and spoken to over stdio."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="Prefix for its tool names.")
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    allow: frozenset[str] | None = None
    """Tool names (without prefix) to offer; ``None`` offers every tool the server lists."""
    tools: dict[str, McpToolPolicy] = {}
    """Per-tool policy, by name without prefix."""
    default: McpToolPolicy = McpToolPolicy()
    timeout_seconds: float = 60.0


class McpArguments(BaseModel):
    """Tool input is checked against the server's own JSON Schema by the server; the
    model's arguments pass through as given."""

    model_config = ConfigDict(extra="allow")


def result_text(result: CallToolResult) -> str:
    """The text of an MCP tool result. Non-text content is named, not dropped silently."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content omitted]")
    return "\n".join(parts) if parts else "(no output)"


class McpToolset:
    """A live connection to one MCP server, and its tools as Kairos ``Tool`` objects.

    Use as an async context manager: entering starts the server process and lists
    its tools; leaving stops it.
    """

    def __init__(self, config: McpServerConfig, *, client: Client | None = None) -> None:
        self.config = config
        self._client = client or Client(
            StdioServerParameters(
                command=config.command, args=list(config.args), env=dict(config.env) or None
            ),
            read_timeout_seconds=config.timeout_seconds,
        )
        self._stack = AsyncExitStack()
        self.tools: list[Tool[McpArguments]] = []

    async def __aenter__(self) -> Self:
        await self._stack.enter_async_context(self._client)
        listing = await self._client.list_tools()
        offered = [
            t for t in listing.tools if self.config.allow is None or t.name in self.config.allow
        ]
        self.tools = [self._wrap(t.name, t.description or t.name, t.input_schema) for t in offered]
        log.info("mcp_connected", server=self.config.name, tools=[t.spec.name for t in self.tools])
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    def _wrap(self, name: str, description: str, schema: dict[str, Any]) -> Tool[McpArguments]:
        policy = self.config.tools.get(name, self.config.default)
        prefixed = f"{self.config.name}_{name}"
        client = self._client

        async def call(args: McpArguments, context: ToolContext) -> ToolOutput:
            arguments: dict[str, Any] = args.model_dump()
            try:
                result = await client.call_tool(name, arguments)
            except (
                Exception
            ) as error:  # a crashed or hung server is a tool error, not a run failure
                log.warning("mcp_call_failed", tool=prefixed, error=str(error))
                return ToolOutput(content=f"{prefixed} failed: {error}", is_error=True)
            return ToolOutput(content=result_text(result), is_error=result.is_error)

        spec = ToolSpec(
            name=prefixed,
            description=description,
            input_schema=_schema(schema),
            risk=policy.risk,
            idempotent=policy.idempotent,
            provenance=policy.provenance,
        )
        return Tool(spec=spec, input_model=McpArguments, function=call)


def _schema(schema: dict[str, Any]) -> dict[str, JsonValue]:
    """MCP schemas are JSON Schema objects already; make sure the top level is one."""
    if schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    return {key: value for key, value in schema.items() if key != "$schema"}


async def open_toolsets(
    stack: AsyncExitStack, configs: Sequence[McpServerConfig]
) -> list[McpToolset]:
    """Connect every configured server, skipping (and logging) any that fail to start,
    so one broken server never takes the agent down."""
    toolsets: list[McpToolset] = []
    for config in configs:
        try:
            toolsets.append(await stack.enter_async_context(McpToolset(config)))
        except Exception as error:
            log.error("mcp_unavailable", server=config.name, error=str(error))
    return toolsets
