"""Subagents: isolated context, read-only tools, a budget, and a compact result."""

from kairos_domain.content import Provenance
from kairos_domain.events import RunFinished, ToolCallResult
from kairos_domain.run import RunRequest, StopReason, ToolCallRecord
from kairos_domain.tools import ToolContext
from kairos_domain.usage import Budget
from kairos_harness import AgentLoop, InMemoryCheckpointer
from kairos_harness.subagents import ReadOnlyTools, SubagentProfile, Subagents
from kairos_harness.testing import (
    ScriptedProvider,
    StaticToolExecutor,
    simple_spec,
    text_turn,
    tool_turn,
)
from kairos_tools import ToolRegistry
from kairos_tools.subagent import delegate_tool


async def search(call: ToolCallRecord, context: ToolContext) -> str:
    return f"results for {call.input.get('query')}: https://fares.example/yyz-cjb $1,250"


async def send(call: ToolCallRecord, context: ToolContext) -> str:
    return "sent"


def base_tools() -> StaticToolExecutor:
    return StaticToolExecutor(
        [
            (simple_spec("web_search", provenance=Provenance.UNTRUSTED), search),
            (simple_spec("notify_me", risk="write_external"), send),
        ]
    )


RESEARCH = SubagentProfile(
    name="research",
    description="Researches a question on the web.",
    tools=frozenset({"web_search", "notify_me"}),  # notify_me is listed but never offered
    budget=Budget(max_steps=4, max_tool_calls=3),
)


def test_children_only_ever_see_allowed_read_tools() -> None:
    view = ReadOnlyTools(base_tools(), frozenset({"web_search", "notify_me"}))
    assert [s.name for s in view.specs()] == ["web_search"]


async def test_the_parent_delegates_and_gets_a_compact_untrusted_result() -> None:
    provider = ScriptedProvider(
        [
            # parent: delegate
            tool_turn(
                ("p1", "delegate", {"profile": "research", "task": "Cheapest YYZ-CJB fare?"})
            ),
            # child: search, then answer
            tool_turn(("c1", "web_search", {"query": "YYZ CJB fares December"})),
            text_turn("Cheapest found: $1,250 ([fares](https://fares.example/yyz-cjb))."),
            # parent: final answer
            text_turn("The cheapest fare is $1,250."),
        ]
    )
    tools = base_tools()
    subagents = Subagents(provider=provider, tools=tools, profiles=[RESEARCH], model="scripted")
    parent_tools = ToolRegistry([delegate_tool(subagents)])
    spec = parent_tools.specs()[0]
    assert spec.provenance is Provenance.UNTRUSTED
    assert spec.input_schema["properties"]["profile"]["enum"] == ["research"]  # type: ignore[index]  # JSON schema dict

    loop = AgentLoop(provider=provider, tools=parent_tools, checkpointer=InMemoryCheckpointer())
    events = [
        e async for e in loop.run(RunRequest(run_id="r1", input="Find me a fare", model="scripted"))
    ]
    finished = next(e for e in events if isinstance(e, RunFinished))
    assert finished.stop_reason is StopReason.COMPLETED

    (delegated,) = (e for e in events if isinstance(e, ToolCallResult))
    assert delegated.output.startswith("Cheapest found: $1,250")
    assert "[subagent r1.sub.p1: 1 tool calls" in delegated.output

    child_request = provider.requests[1]
    # Isolated: the child sees only the task, with its own instructions and tools.
    assert [m.role for m in child_request.messages] == ["user"]
    assert "Cheapest YYZ-CJB fare?" in str(child_request.messages[0].content)
    assert child_request.system.startswith("You are a focused subagent")
    assert [t.name for t in child_request.tools] == ["web_search"]
    assert [c.name for c in tools.calls] == ["web_search"]


async def test_a_child_that_runs_out_of_budget_says_so() -> None:
    provider = ScriptedProvider(
        [tool_turn((f"c{i}", "web_search", {"query": str(i)})) for i in range(10)]
    )
    subagents = Subagents(
        provider=provider, tools=base_tools(), profiles=[RESEARCH], model="scripted"
    )
    context = ToolContext(run_id="r9", tool_call_id="p9", idempotency_key="p9")
    result = await subagents.run("research", "Dig forever", parent=context)
    assert result.stop_reason is StopReason.BUDGET_EXCEEDED
    assert "stopped early: budget_exceeded" in result.for_parent()
    assert result.tool_calls <= 3


async def test_an_unknown_profile_is_a_tool_error() -> None:
    subagents = Subagents(
        provider=ScriptedProvider([]), tools=base_tools(), profiles=[RESEARCH], model="scripted"
    )
    out = await delegate_tool(subagents)(
        {"profile": "shopping", "task": "x"},
        ToolContext(run_id="r", tool_call_id="c", idempotency_key="c"),
    )
    assert out.is_error
    assert "unknown profile 'shopping'" in out.content
