"""``notify_me``: the agent sends the run's user a message (email and push).

It can only reach the user whose run it is (ADR-018): the recipient is that user's own
verified address and browsers, never an address the model chooses. Under the
strict policy every call waits for approval first. The message goes through the
outbox with the tool call's idempotency key as dedupe key, so a retried call can
never send twice.
"""

from collections.abc import Awaitable, Callable, Sequence

from pydantic import BaseModel, Field

from kairos_domain.governance import ChannelName, Notification, NotificationKind
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_tools import Tool, tool

type Enqueue = Callable[[Notification, str, str], Awaitable[None]]
"""``(notification, dedupe_key, user_id)``: queue a message for one user."""


class NotifyInput(BaseModel):
    title: str = Field(min_length=1, max_length=150, description="Subject line.")
    body: str = Field(min_length=1, max_length=10_000, description="The message, plain text.")


def delivery_report(channels: Sequence[ChannelName]) -> str:
    """What the model is told, so it never claims a delivery that did not happen."""
    email, push = "email" in channels, "push" in channels
    if email and push:
        return "Queued for delivery to the user by email and to browsers that enabled push."
    if email:
        return "Queued for delivery to the user by email (browser push is not set up)."
    if push:
        return "Queued for browser push to the user (email is not set up)."
    return (
        "Recorded, but NOT delivered: neither email nor push is set up, so the message is "
        "only in the Kairos log (Settings > Recent notifications). Tell the user it did not "
        "reach them."
    )


def notify_tool(enqueue: Enqueue, channels: Sequence[ChannelName]) -> Tool[NotifyInput]:
    """``channels`` are the configured ones; the message goes only there."""
    report = delivery_report(channels)

    @tool(name="notify_me", risk=ToolRisk.WRITE_EXTERNAL)
    async def notify_me(args: NotifyInput, context: ToolContext) -> ToolOutput:
        """Send the user a message by email and browser push, for example a summary
        they asked to receive. It only reaches the user themselves and needs their
        approval each time."""
        notification = Notification(
            kind=NotificationKind.MESSAGE, title=args.title, body=args.body, channels=list(channels)
        )
        await enqueue(notification, f"notify:{context.idempotency_key}", context.user_id)
        return ToolOutput(content=report)

    return notify_me
