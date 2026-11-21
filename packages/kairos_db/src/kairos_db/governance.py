"""Approvals, audit log, notification outbox, push subscriptions and settings."""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import JsonValue
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kairos_domain.errors import KairosError
from kairos_domain.governance import (
    Actor,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Notification,
    NotificationKind,
    NotificationStatus,
    PushKeys,
    PushSubscription,
    Recipient,
    StoredNotification,
)

KILL_SWITCH = "agent_actions_enabled"


class AlreadyDecidedError(KairosError):
    pass


# --- shared inserts (used inside other stores' transactions) ----------------------------


async def insert_notification(
    connection: AsyncConnection,
    notification: Notification,
    *,
    user_id: str,
    dedupe_key: str | None = None,
) -> None:
    """Queue a notification for ``user_id`` in the caller's transaction (the outbox
    pattern)."""
    await connection.execute(
        text(
            "INSERT INTO notifications (kind, payload, status, dedupe_key, user_id) "
            "VALUES (:kind, CAST(:payload AS jsonb), 'pending', :key, :user) "
            "ON CONFLICT (dedupe_key) DO NOTHING"
        ),
        {
            "user": user_id,
            "kind": notification.kind.value,
            "payload": notification.model_dump_json(),
            "key": dedupe_key,
        },
    )


async def insert_audit(
    connection: AsyncConnection,
    *,
    user_id: str | None,
    actor: Actor,
    action: str,
    subject: str,
    run_id: str | None = None,
    detail: Mapping[str, JsonValue] | None = None,
) -> None:
    """Append to ``user_id``'s audit log. ``None`` takes the owner of ``run_id``; with
    neither, the insert fails (``user_id`` is ``NOT NULL``)."""
    await connection.execute(
        text(
            "INSERT INTO audit_log (actor, action, subject, run_id, detail, user_id) "
            "VALUES (:actor, :action, :subject, :run, CAST(:detail AS jsonb), "
            "COALESCE(CAST(:user AS text), (SELECT user_id FROM runs WHERE id = :run)))"
        ),
        {
            "user": user_id,
            "actor": actor,
            "action": action,
            "subject": subject,
            "run": run_id,
            "detail": json.dumps(dict(detail or {})),
        },
    )


def approval_id(run_id: str, tool_call_id: str) -> str:
    return f"apr_{run_id.removeprefix('run_')[:16]}_{tool_call_id[-12:]}"


async def insert_approval(
    connection: AsyncConnection,
    *,
    run_id: str,
    tool_call_id: str,
    name: str,
    tool_input: Mapping[str, JsonValue],
    reason: str,
    risk: str,
) -> None:
    """Record a pending approval, notify the owner, and audit it. Idempotent."""
    result = await connection.execute(
        text(
            "INSERT INTO approvals (id, run_id, tool_call_id, name, input, reason, risk, status) "
            "VALUES (:id, :run, :call, :name, CAST(:input AS jsonb), :reason, :risk, 'pending') "
            "ON CONFLICT (run_id, tool_call_id) DO NOTHING"
        ),
        {
            "id": approval_id(run_id, tool_call_id),
            "run": run_id,
            "call": tool_call_id,
            "name": name,
            "input": json.dumps(dict(tool_input)),
            "reason": reason,
            "risk": risk,
        },
    )
    if result.rowcount != 1:
        return
    owner = (
        await connection.execute(text("SELECT user_id FROM runs WHERE id = :run"), {"run": run_id})
    ).scalar_one()
    await insert_notification(
        connection,
        Notification(
            kind=NotificationKind.APPROVAL,
            title=f"Approval needed: {name}",
            body=f"{reason}.\n\nInput: {json.dumps(dict(tool_input))[:1000]}",
            url="/inbox",
        ),
        user_id=owner,
        dedupe_key=f"approval:{run_id}:{tool_call_id}",
    )
    await insert_audit(
        connection,
        user_id=owner,
        actor="agent",
        action="approval.requested",
        subject=name,
        run_id=run_id,
        detail={"tool_call_id": tool_call_id, "reason": reason},
    )


def _approval(row: RowMapping) -> Approval:
    return Approval.model_validate(dict(row))


_APPROVAL_COLUMNS = (
    "a.id, a.run_id, a.tool_call_id, a.name, a.input, a.reason, a.risk, a.status, a.note, "
    "a.created_at, a.decided_at"
)
_FROM_APPROVALS = "FROM approvals a JOIN runs r ON r.id = a.run_id"
"""Approvals belong to their run's owner."""


class ApprovalStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_approvals(
        self, *, owner: str, status: ApprovalStatus | None = None, limit: int = 100
    ) -> list[Approval]:
        where = "AND a.status = :status" if status else ""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    f"SELECT {_APPROVAL_COLUMNS} {_FROM_APPROVALS} "
                    f"WHERE r.user_id = :owner {where} "
                    "ORDER BY a.created_at DESC LIMIT :limit"
                ),
                {"owner": owner, "status": status.value if status else None, "limit": limit},
            )
            return [_approval(r) for r in rows.mappings()]

    async def for_run(self, run_id: str, *, owner: str | None) -> list[Approval]:
        """A run's approvals. ``owner=None`` is the worker, which acts for every user."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    f"SELECT {_APPROVAL_COLUMNS} {_FROM_APPROVALS} "
                    "WHERE a.run_id = :run "
                    "AND (CAST(:owner AS text) IS NULL OR r.user_id = :owner)"
                ),
                {"run": run_id, "owner": owner},
            )
            return [_approval(r) for r in rows.mappings()]

    async def decide(
        self, approval_id_: str, *, owner: str, approve: bool, note: str | None, now: datetime
    ) -> Approval:
        """Decide one approval. When the run has nothing else pending, it is queued
        again so a worker resumes it with this decision. Another user's approval
        raises ``KeyError``, exactly like a missing one."""
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        async with self._engine.begin() as connection:
            current = (
                await connection.execute(
                    text(
                        f"SELECT a.status {_FROM_APPROVALS} "
                        "WHERE a.id = :id AND r.user_id = :owner FOR UPDATE OF a"
                    ),
                    {"id": approval_id_, "owner": owner},
                )
            ).one_or_none()
            if current is None:
                raise KeyError(approval_id_)
            if current.status != ApprovalStatus.PENDING.value:
                raise AlreadyDecidedError(f"approval {approval_id_} is already {current.status}")
            row = (
                (
                    await connection.execute(
                        text(
                            "UPDATE approvals a SET status = :status, note = :note, "  # noqa: S608
                            "decided_at = :now WHERE a.id = :id AND a.status = 'pending' "
                            f"RETURNING {_APPROVAL_COLUMNS}"
                        ),
                        {"status": status.value, "note": note, "now": now, "id": approval_id_},
                    )
                )
                .mappings()
                .one()
            )
            approval = _approval(row)
            remaining = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM approvals WHERE run_id = :run AND status = 'pending'"
                    ),
                    {"run": approval.run_id},
                )
            ).scalar_one()
            if remaining == 0:
                await connection.execute(
                    text(
                        "UPDATE runs SET status = 'queued', updated_at = now() "
                        "WHERE id = :run AND status = 'paused'"
                    ),
                    {"run": approval.run_id},
                )
            await insert_audit(
                connection,
                user_id=owner,
                actor="user",
                action=f"approval.{status.value}",
                subject=approval.name,
                run_id=approval.run_id,
                detail={"approval_id": approval.id, "note": note},
            )
        return approval


class AuditStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self,
        *,
        owner: str | None,
        actor: Actor,
        action: str,
        subject: str,
        run_id: str | None = None,
        detail: Mapping[str, JsonValue] | None = None,
    ) -> None:
        """``owner=None`` takes the owner of ``run_id`` (agent actions)."""
        async with self._engine.begin() as connection:
            await insert_audit(
                connection,
                user_id=owner,
                actor=actor,
                action=action,
                subject=subject,
                run_id=run_id,
                detail=detail,
            )

    async def recent(
        self, *, owner: str, limit: int = 200, before_id: int | None = None
    ) -> list[AuditEntry]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT id, at, actor, action, subject, run_id, detail FROM audit_log "
                    "WHERE user_id = :owner "
                    "AND (CAST(:before AS bigint) IS NULL OR id < :before) "
                    "ORDER BY id DESC LIMIT :limit"
                ),
                {"owner": owner, "before": before_id, "limit": limit},
            )
            return [AuditEntry.model_validate(dict(r)) for r in rows.mappings()]


class SettingsStore:
    """Small key-value settings for the whole deployment. The kill switch lives here."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def actions_enabled(self) -> bool:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT value FROM app_settings WHERE key = :key"), {"key": KILL_SWITCH}
                )
            ).one_or_none()
        return True if row is None else bool(row.value)

    async def set_actions_enabled(self, enabled: bool, *, by: str, now: datetime) -> int:
        """Flip the kill switch (an admin, ``by``). Turning actions off also pauses
        every active job, everyone's. Returns how many jobs were paused."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO app_settings (key, value, updated_at) "
                    "VALUES (:key, CAST(:value AS jsonb), :now) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = :now"
                ),
                {"key": KILL_SWITCH, "value": json.dumps(enabled), "now": now},
            )
            paused = 0
            if not enabled:
                result = await connection.execute(
                    text(
                        "UPDATE jobs SET status = 'paused', updated_at = :now "
                        "WHERE status = 'active'"
                    ),
                    {"now": now},
                )
                paused = result.rowcount
            await insert_audit(
                connection,
                user_id=by,
                actor="user",
                action="kill_switch.off" if enabled else "kill_switch.on",
                subject="agent actions",
                detail={"jobs_paused": paused},
            )
        return paused


class NotificationStore:
    """The outbox (``kairos_notify.Outbox``), its history, and push subscriptions."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(
        self, notification: Notification, *, owner: str, dedupe_key: str | None = None
    ) -> None:
        async with self._engine.begin() as connection:
            await insert_notification(
                connection, notification, user_id=owner, dedupe_key=dedupe_key
            )

    async def claim(
        self, *, now: datetime, limit: int, ttl_seconds: float
    ) -> Sequence[StoredNotification]:
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                text(
                    """
                    UPDATE notifications SET lease_until = :until
                     WHERE id IN (
                           SELECT id FROM notifications
                            WHERE status = 'pending' AND next_attempt_at <= :now
                              AND (lease_until IS NULL OR lease_until < :now)
                            ORDER BY id
                              FOR UPDATE SKIP LOCKED
                            LIMIT :limit)
                    RETURNING id, user_id, payload, status, attempts, results, created_at, sent_at
                    """
                ),
                {"until": now + timedelta(seconds=ttl_seconds), "now": now, "limit": limit},
            )
            return [_stored(r) for r in rows.mappings()]

    async def recipient(self, user_id: str) -> Recipient:
        """Where ``user_id``'s notifications may go: an email address only when SES
        has verified it, and never for a disabled user."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT email, email_verified, disabled FROM users WHERE id = :id"),
                    {"id": user_id},
                )
            ).one_or_none()
        if row is None or row.disabled:
            return Recipient(user_id=user_id, email=None, active=False)
        return Recipient(user_id=user_id, email=row.email if row.email_verified else None)

    async def finish(
        self,
        notification_id: int,
        *,
        results: Mapping[str, str],
        sent: bool,
        retry_at: datetime | None,
        now: datetime,
    ) -> None:
        if retry_at is not None:
            status = NotificationStatus.PENDING
        else:
            status = NotificationStatus.SENT if sent else NotificationStatus.FAILED
        async with self._engine.begin() as connection:
            owner = (
                await connection.execute(
                    text(
                        "UPDATE notifications SET status = :status, attempts = attempts + 1, "
                        "results = CAST(:results AS jsonb), lease_until = NULL, "
                        "next_attempt_at = COALESCE(:retry, next_attempt_at), "
                        "sent_at = CASE WHEN :status = 'sent' THEN :now ELSE sent_at END "
                        "WHERE id = :id RETURNING user_id"
                    ),
                    {
                        "status": status.value,
                        "results": json.dumps(dict(results)),
                        "retry": retry_at,
                        "now": now,
                        "id": notification_id,
                    },
                )
            ).scalar_one_or_none()
            if status is NotificationStatus.SENT and owner is not None:
                await insert_audit(
                    connection,
                    user_id=owner,
                    actor="system",
                    action="notification.sent",
                    subject=str(notification_id),
                    detail={k: v for k, v in results.items()},
                )

    async def recent(self, *, owner: str, limit: int = 50) -> list[StoredNotification]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT id, user_id, payload, status, attempts, results, created_at, sent_at "
                    "FROM notifications WHERE user_id = :owner ORDER BY id DESC LIMIT :limit"
                ),
                {"owner": owner, "limit": limit},
            )
            return [_stored(r) for r in rows.mappings()]

    async def subscriptions(self, owner: str) -> list[PushSubscription]:
        """The browsers ``owner`` enabled push on."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT endpoint, keys FROM push_subscriptions WHERE user_id = :owner"),
                {"owner": owner},
            )
            return [
                PushSubscription(endpoint=r.endpoint, keys=PushKeys.model_validate(r.keys))
                for r in rows
            ]

    async def subscribe(self, subscription: PushSubscription, *, owner: str) -> None:
        """A browser belongs to whoever subscribed it last."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO push_subscriptions (endpoint, keys, user_id) "
                    "VALUES (:endpoint, CAST(:keys AS jsonb), :owner) "
                    "ON CONFLICT (endpoint) DO UPDATE SET keys = EXCLUDED.keys, "
                    "user_id = EXCLUDED.user_id"
                ),
                {
                    "endpoint": subscription.endpoint,
                    "keys": subscription.keys.model_dump_json(),
                    "owner": owner,
                },
            )

    async def forget(self, endpoint: str, *, owner: str | None) -> None:
        """Drop a subscription. ``owner=None`` is the push channel removing an endpoint
        the browser's push service reported gone."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM push_subscriptions WHERE endpoint = :endpoint "
                    "AND (CAST(:owner AS text) IS NULL OR user_id = :owner)"
                ),
                {"endpoint": endpoint, "owner": owner},
            )


def _stored(row: RowMapping) -> StoredNotification:
    data: dict[str, Any] = dict(row)
    notification = Notification.model_validate(data.pop("payload"))
    return StoredNotification.model_validate({**data, "notification": notification})
