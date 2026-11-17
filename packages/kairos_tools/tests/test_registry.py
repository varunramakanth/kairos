import pytest
from pydantic import BaseModel, Field

from kairos_domain.content import Provenance
from kairos_domain.run import ToolCallRecord
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_tools import ToolRegistry, tool

CONTEXT = ToolContext(run_id="run_1", tool_call_id="t1", idempotency_key="t1")


class SearchInput(BaseModel):
    query: str = Field(description="What to search for")
    limit: int = 5


@tool(provenance=Provenance.UNTRUSTED)
async def web_search(args: SearchInput, context: ToolContext) -> str:
    """Search the web and return the top results."""
    return f"{args.limit} results for {args.query} ({context.idempotency_key})"


class EmailInput(BaseModel):
    to: str


@tool(name="email.send", risk=ToolRisk.WRITE_EXTERNAL, idempotent=False)
async def send_email(args: EmailInput, context: ToolContext) -> ToolOutput:
    """Send an email."""
    return ToolOutput(content=f"refused to mail {args.to}", is_error=True)


def test_decorator_builds_the_spec_from_the_function() -> None:
    spec = web_search.spec
    assert spec.name == "web_search"
    assert spec.description == "Search the web and return the top results."
    assert spec.provenance is Provenance.UNTRUSTED
    assert spec.risk is ToolRisk.READ
    assert spec.idempotent
    assert spec.input_schema["type"] == "object"
    assert spec.input_schema["required"] == ["query"]

    assert send_email.spec.name == "email.send"
    assert send_email.spec.risk is ToolRisk.WRITE_EXTERNAL
    assert not send_email.spec.idempotent


async def test_registry_validates_input_and_runs_the_tool() -> None:
    registry = ToolRegistry([web_search, send_email])
    assert [s.name for s in registry.specs()] == ["web_search", "email.send"]

    ok = await registry.execute(
        ToolCallRecord(id="t1", name="web_search", input={"query": "fares"}), CONTEXT
    )
    assert ok == ToolOutput(content="5 results for fares (t1)")

    passthrough = await registry.execute(
        ToolCallRecord(id="t2", name="email.send", input={"to": "raj@example.com"}), CONTEXT
    )
    assert passthrough.is_error


async def test_invalid_input_is_reported_to_the_model_not_raised() -> None:
    registry = ToolRegistry([web_search])
    bad = await registry.execute(
        ToolCallRecord(id="t1", name="web_search", input={"limit": "many"}), CONTEXT
    )
    assert bad.is_error
    assert "query: Field required" in bad.content
    assert "limit:" in bad.content

    missing = await registry.execute(ToolCallRecord(id="t1", name="nope", input={}), CONTEXT)
    assert missing == ToolOutput(content="unknown tool: nope", is_error=True)


def test_registration_errors_are_caught_at_import_time() -> None:
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([web_search, web_search])

    with pytest.raises(TypeError, match="Pydantic model"):

        @tool()  # pyright: ignore[reportArgumentType]  # the wrong type is the point
        async def bad_input(args: str, context: ToolContext) -> str:  # pyright: ignore[reportUnusedFunction]
            """Takes a plain string."""
            return args

    with pytest.raises(TypeError, match="docstring"):

        @tool()
        async def undocumented(args: SearchInput, context: ToolContext) -> str:  # pyright: ignore[reportUnusedFunction]
            return args.query


async def test_current_time_tool_uses_the_injected_clock_and_handles_dst() -> None:
    from datetime import UTC, datetime

    from kairos_tools.builtin import current_time_tool

    # 06:30 UTC on 1 Nov 2026 is 01:30 in Toronto, after clocks fell back at 02:00 EDT.
    clock_tool = current_time_tool(lambda: datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    registry = ToolRegistry([clock_tool])

    toronto = await registry.execute(
        ToolCallRecord(id="t1", name="current_time", input={}), CONTEXT
    )
    assert toronto.content == "2026-11-01T01:30:00-05:00 (America/Toronto)"

    bad = await registry.execute(
        ToolCallRecord(id="t2", name="current_time", input={"timezone": "Mars/Olympus"}), CONTEXT
    )
    assert bad == ToolOutput(content="unknown timezone: Mars/Olympus", is_error=True)
