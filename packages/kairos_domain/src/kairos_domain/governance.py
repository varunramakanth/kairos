"""Approvals, the audit log and notifications: what a human sees and decides."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue

from kairos_domain.base import DomainModel
from kairos_domain.tools import ToolRisk


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class Approval(DomainModel):
    """A tool call waiting for (or decided by) a human."""

    id: str
    run_id: str
    tool_call_id: str
    name: str
    input: dict[str, JsonValue]
    reason: str
    risk: ToolRisk
    status: ApprovalStatus
    note: str | None
    created_at: datetime
    decided_at: datetime | None


type Actor = Literal["agent", "user", "system"]


class AuditEntry(DomainModel):
    """One thing that happened with a side effect, or a human decision."""

    id: int
    at: datetime
    actor: Actor
    action: str
    """Dotted verb, e.g. ``tool.executed``, ``approval.approved``, ``job.paused``."""
    subject: str
    """What it applied to: a tool name, job id, approval id, ..."""
    run_id: str | None
    detail: dict[str, JsonValue]


class NotificationKind(StrEnum):
    ALERT = "alert"
    APPROVAL = "approval"
    DIGEST = "digest"
    MESSAGE = "message"
    """Sent by the agent with ``notify_me`` (after approval)."""
    TEST = "test"


type ChannelName = Literal["console", "email", "push"]


class Notification(DomainModel):
    kind: NotificationKind
    title: str = Field(max_length=200)
    body: str = Field(max_length=20_000)
    url: str | None = None
    """A link back into the app, relative to the web UI (``/tasks/job_...``)."""
    channels: list[ChannelName] = Field(default_factory=lambda: ["console", "email", "push"])


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class StoredNotification(DomainModel):
    id: int
    notification: Notification
    status: NotificationStatus
    attempts: int
    results: dict[str, str]
    """Per channel: ``sent``, ``skipped (...)`` or the last error."""
    created_at: datetime
    sent_at: datetime | None


class PushKeys(DomainModel):
    p256dh: str
    auth: str


class PushSubscription(DomainModel):
    """What the browser's ``PushManager.subscribe()`` returns."""

    endpoint: str = Field(pattern=r"^https://")
    keys: PushKeys


class DigestJob(DomainModel):
    """One job's week, for the digest."""

    name: str
    kind: str
    runs: int
    failures: int
    status: str
    latest: str | None


class DigestData(DomainModel):
    since: datetime
    until: datetime
    alerts: list[str]
    jobs: list[DigestJob]
    pending_approvals: int
    runs: int
    cost_usd: float
