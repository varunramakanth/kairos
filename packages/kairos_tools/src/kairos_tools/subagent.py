"""The ``delegate`` tool: hand a focused, read-only task to a subagent."""

from typing import Any

from pydantic import BaseModel, Field

from kairos_domain.content import Provenance
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_harness.subagents import Subagents
from kairos_tools.registry import Tool, tool

DELEGATE_TOOL = "delegate"


def delegate_tool(subagents: Subagents) -> Tool[Any]:
    """``delegate``, with the profiles listed in its description and input schema."""
    names = tuple(subagents.profiles)
    if not names:
        raise ValueError("delegate needs at least one subagent profile")
    listing = "\n".join(f"- {p.name}: {p.description}" for p in subagents.profiles.values())

    class DelegateInput(BaseModel):
        profile: str = Field(
            description="Which kind of subagent to use.",
            json_schema_extra={"enum": list(names)},
        )
        task: str = Field(
            min_length=1,
            max_length=4_000,
            description="The whole task, self-contained: the subagent sees nothing else.",
        )

    @tool(
        name=DELEGATE_TOOL,
        description=(
            "Hand a focused, read-only task to a subagent with its own context and a small "
            "toolset, and get back a compact result. Use it for self-contained research "
            "that would otherwise take many tool calls (comparing sources, digging through "
            "several pages). The subagent cannot take actions or ask questions.\n\n"
            f"Profiles:\n{listing}"
        ),
        risk=ToolRisk.READ,
        idempotent=True,
        provenance=Provenance.UNTRUSTED,
    )
    async def delegate(args: DelegateInput, context: ToolContext) -> ToolOutput:
        if args.profile not in subagents.profiles:
            return ToolOutput(
                content=f"unknown profile {args.profile!r}; use one of {list(names)}", is_error=True
            )
        result = await subagents.run(args.profile, args.task, parent=context)
        return ToolOutput(content=result.for_parent())

    return delegate
