"""``update_plan``: the visible plan checklist.

The tool has no side effect beyond the run's own history. The UI renders the most
recent ``update_plan`` call of a run as a checklist, so the plan the model is
following is always on screen.
"""

from typing import Literal

from pydantic import BaseModel, Field

from kairos_domain.tools import ToolContext, ToolOutput
from kairos_tools.registry import Tool, tool


class PlanItem(BaseModel):
    step: str = Field(min_length=1, max_length=200, description="One short imperative step.")
    status: Literal["pending", "in_progress", "completed"]


class UpdatePlanInput(BaseModel):
    items: list[PlanItem] = Field(min_length=1, max_length=12)


def plan_tool() -> Tool[UpdatePlanInput]:
    @tool()
    async def update_plan(args: UpdatePlanInput, context: ToolContext) -> ToolOutput:
        """Show the user a checklist of the steps you are taking. Use it for tasks
        that need three or more steps: call it before starting with the whole plan,
        then again with the full updated list whenever a step's status changes.
        Keep at most one step in_progress. Skip it for simple questions."""
        done = sum(1 for item in args.items if item.status == "completed")
        return ToolOutput(content=f"Plan updated: {done} of {len(args.items)} steps done.")

    return update_plan
