"""The generic MCP client against an in-process MCP server: Kairos, not the server,
decides risk and trust, and server failures become tool errors."""

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from kairos_domain.content import Provenance
from kairos_domain.run import ToolCallRecord
from kairos_domain.tools import ToolContext, ToolRisk
from kairos_tools import ToolRegistry
from kairos_tools.mcp import McpServerConfig, McpToolPolicy, McpToolset


def demo_server() -> MCPServer:
    server: MCPServer = MCPServer("demo")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def lookup(city: str) -> str:
        """Look up a city."""
        return f"{city}: sunny"

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))  # the server claims read-only
    async def send(to: str) -> str:
        """Send something."""
        return f"sent to {to}"

    @server.tool()
    async def broken() -> str:
        """Always fails."""
        raise ToolError("upstream is down")

    @server.tool()
    async def hidden() -> str:
        """Not offered to the agent."""
        return "secret"

    return server


CONFIG = McpServerConfig(
    name="demo",
    command="unused-in-process",
    allow=frozenset({"lookup", "send", "broken"}),
    tools={"lookup": McpToolPolicy(risk=ToolRisk.READ, idempotent=True)},
)


def ctx() -> ToolContext:
    return ToolContext(run_id="r", tool_call_id="c", idempotency_key="c")


async def test_tools_are_prefixed_filtered_and_trusted_only_as_configured() -> None:
    async with McpToolset(CONFIG, client=Client(demo_server())) as toolset:
        specs = {t.spec.name: t.spec for t in toolset.tools}
        assert set(specs) == {"demo_lookup", "demo_send", "demo_broken"}  # hidden is not offered
        assert specs["demo_lookup"].risk is ToolRisk.READ
        # The server's read-only hint is ignored: unlisted tools need approval.
        assert specs["demo_send"].risk is ToolRisk.WRITE_EXTERNAL
        assert all(s.provenance is Provenance.UNTRUSTED for s in specs.values())
        assert specs["demo_lookup"].input_schema["properties"] == {
            "city": {"title": "City", "type": "string"}
        }

        registry = ToolRegistry(toolset.tools)
        call = ToolCallRecord(id="c", name="demo_lookup", input={"city": "Toronto"})
        out = await registry.execute(call, ctx())
        assert (out.content, out.is_error) == ("Toronto: sunny", False)

        failed = await registry.execute(ToolCallRecord(id="d", name="demo_broken", input={}), ctx())
        assert failed.is_error
        assert "upstream is down" in failed.content


async def test_a_server_that_fails_mid_call_is_a_tool_error_not_a_crash() -> None:
    toolset = McpToolset(CONFIG, client=Client(demo_server()))
    async with toolset:
        lookup = next(t for t in toolset.tools if t.spec.name == "demo_lookup")
    # The connection is closed now: calling must report an error, not raise.
    out = await lookup({"city": "Toronto"}, ctx())
    assert out.is_error
    assert out.content.startswith("demo_lookup failed:")
