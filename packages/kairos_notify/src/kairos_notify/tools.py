"""``notify_me``: the agent sends the owner a message (email and push).

It can only reach the owner: the recipients are fixed by configuration. Under the
strict policy every call waits for approval first. The message goes through the
outbox with the tool call's idempotency key as dedupe key, so a retried call can
never send twice.
"""

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from kairos_domain.governance import Notification, NotificationKind
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_tools import Tool, tool

type Enqueue = Callable[[Notification, str], Awaitable[None]]


class NotifyInput(BaseModel):
    title: str = Field(min_length=1, max_length=150, description="Subject line.")
    body: str = Field(min_length=1, max_length=10_000, description="The message, plain text.")


def notify_tool(enqueue: Enqueue) -> Tool[NotifyInput]:
    @tool(name="notify_me", risk=ToolRisk.WRITE_EXTERNAL)
    async def notify_me(args: NotifyInput, context: ToolContext) -> ToolOutput:
        """Send the user a message by email and browser push, for example a summary
        they asked to receive. It only reaches the user themselves and needs their
        approval each time."""
        notification = Notification(kind=NotificationKind.MESSAGE, title=args.title, body=args.body)
        await enqueue(notification, f"notify:{context.idempotency_key}")
        return ToolOutput(content="Message queued for delivery to the user.")

    return notify_me
