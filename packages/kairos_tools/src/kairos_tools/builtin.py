"""Small built-in tools with no external dependencies."""

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from kairos_domain.tools import ToolContext, ToolOutput
from kairos_tools.registry import Tool, tool


class CurrentTimeInput(BaseModel):
    timezone: str = Field(
        default="America/Toronto",
        description="IANA timezone name, for example America/Toronto or Asia/Kolkata.",
    )


def current_time_tool(
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Tool[CurrentTimeInput]:
    """Build the ``current_time`` tool. ``now`` is injectable so tests control the clock."""

    @tool()
    async def current_time(args: CurrentTimeInput, context: ToolContext) -> ToolOutput:
        """Get the current date and time in a timezone. Use this whenever the answer
        depends on today's date or the time of day."""
        try:
            zone = ZoneInfo(args.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ToolOutput(content=f"unknown timezone: {args.timezone}", is_error=True)
        local = now().astimezone(zone)
        return ToolOutput(content=f"{local.isoformat(timespec='seconds')} ({args.timezone})")

    return current_time
