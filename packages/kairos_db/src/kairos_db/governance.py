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
    StoredNotification,
)

KILL_SWITCH = "agent_actions_enabled"


class AlreadyDecidedError(KairosError):
    pass


# --- shared inserts (used inside other stores' transactions) ----------------------------


async def insert_notification(
    connection: AsyncConnection, notification: Notification, *, dedupe_key: str | None = None
) -> None:
    """Queue a notification in the caller's transaction (the outbox pattern)."""
    await connection.execute(
        text(
            "INSERT INTO notifications (kind, payload, status, dedupe_key) "
            "VALUES (:kind, CAST(:payload AS jsonb), 'pending', :key) "
            "ON CONFLICT (dedupe_key) DO NOTHING"
        ),
        {
            "kind": notification.kind.value,
            "payload": notification.model_dump_json(),
            "key": dedupe_key,
        },
    )


async def insert_audit(
    connection: AsyncConnection,
    *,
    actor: Actor,
    action: str,
    subject: str,
    run_id: str | None = None,
    detail: Mapping[str, JsonValue] | None = None,
) -> None:
    await connection.execute(
        text(
            "INSERT INTO audit_log (actor, action, subject, run_id, detail) "
            "VALUES (:actor, :action, :subject, :run, CAST(:detail AS jsonb))"
        ),
        {
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
    await insert_notification(
        connection,
        Notification(
            kind=NotificationKind.APPROVAL,
            title=f"Approval needed: {name}",
            body=f"{reason}.\n\nInput: {json.dumps(dict(tool_input))[:1000]}",
            url="/inbox",
        ),
        dedupe_key=f"approval:{run_id}:{tool_call_id}",
    )
    await insert_audit(
        connection,
        actor="agent",
        action="approval.requested",
        subject=name,
        run_id=run_id,
        detail={"tool_call_id": tool_call_id, "reason": reason},
    )


def _approval(row: RowMapping) -> Approval:
    return Approval.model_validate(dict(row))


_APPROVAL_COLUMNS = (
    "id, run_id, tool_call_id, name, input, reason, risk, status, note, created_at, decided_at"
)


_DECIDE = (  # constant column list, no user input
    "UPDATE approvals SET status = :status, note = :note, decided_at = :now "  # noqa: S608
    f"WHERE id = :id AND status = 'pending' RETURNING {_APPROVAL_COLUMNS}"
)


class ApprovalStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_approvals(
        self, *, status: ApprovalStatus | None = None, limit: int = 100
    ) -> list[Approval]:
        where = "WHERE status = :status" if status else ""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    f"SELECT {_APPROVAL_COLUMNS} FROM approvals {where} "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"status": status.value if status else None, "limit": limit},
            )
            return [_approval(r) for r in rows.mappings()]

    async def for_run(self, run_id: str) -> list[Approval]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE run_id = :run"),  # noqa: S608
                {"run": run_id},
            )
            return [_approval(r) for r in rows.mappings()]

    async def decide(
        self, approval_id_: str, *, approve: bool, note: str | None, now: datetime
    ) -> Approval:
        """Decide one approval. When the run has nothing else pending, it is queued
        again so a worker resumes it with this decision."""
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(_DECIDE),
                        {"status": status.value, "note": note, "now": now, "id": approval_id_},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                exists = (
                    await connection.execute(
                        text("SELECT status FROM approvals WHERE id = :id"), {"id": approval_id_}
                    )
                ).one_or_none()
                if exists is None:
                    raise KeyError(approval_id_)
                raise AlreadyDecidedError(f"approval {approval_id_} is already {exists.status}")
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
        actor: Actor,
        action: str,
        subject: str,
        run_id: str | None = None,
        detail: Mapping[str, JsonValue] | None = None,
    ) -> None:
        async with self._engine.begin() as connection:
            await insert_audit(
                connection,
                actor=actor,
                action=action,
                subject=subject,
                run_id=run_id,
                detail=detail,
            )

    async def recent(self, *, limit: int = 200, before_id: int | None = None) -> list[AuditEntry]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT id, at, actor, action, subject, run_id, detail FROM audit_log "
                    "WHERE (CAST(:before AS bigint) IS NULL OR id < :before) "
                    "ORDER BY id DESC LIMIT :limit"
                ),
                {"before": before_id, "limit": limit},
            )
            return [AuditEntry.model_validate(dict(r)) for r in rows.mappings()]


class SettingsStore:
    """Small key-value settings. The kill switch lives here."""

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

    async def set_actions_enabled(self, enabled: bool, *, now: datetime) -> int:
        """Flip the kill switch. Turning actions off also pauses every active job.
        Returns how many jobs were paused."""
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

    async def enqueue(self, notification: Notification, *, dedupe_key: str | None = None) -> None:
        async with self._engine.begin() as connection:
            await insert_notification(connection, notification, dedupe_key=dedupe_key)

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
                    RETURNING id, payload, status, attempts, results, created_at, sent_at
                    """
                ),
                {"until": now + timedelta(seconds=ttl_seconds), "now": now, "limit": limit},
            )
            return [_stored(r) for r in rows.mappings()]

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
            await connection.execute(
                text(
                    "UPDATE notifications SET status = :status, attempts = attempts + 1, "
                    "results = CAST(:results AS jsonb), lease_until = NULL, "
                    "next_attempt_at = COALESCE(:retry, next_attempt_at), "
                    "sent_at = CASE WHEN :status = 'sent' THEN :now ELSE sent_at END "
                    "WHERE id = :id"
                ),
                {
                    "status": status.value,
                    "results": json.dumps(dict(results)),
                    "retry": retry_at,
                    "now": now,
                    "id": notification_id,
                },
            )
            if status is NotificationStatus.SENT:
                await insert_audit(
                    connection,
                    actor="system",
                    action="notification.sent",
                    subject=str(notification_id),
                    detail={k: v for k, v in results.items()},
                )

    async def recent(self, *, limit: int = 50) -> list[StoredNotification]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT id, payload, status, attempts, results, created_at, sent_at "
                    "FROM notifications ORDER BY id DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [_stored(r) for r in rows.mappings()]

    async def subscriptions(self) -> list[PushSubscription]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(text("SELECT endpoint, keys FROM push_subscriptions"))
            return [
                PushSubscription(endpoint=r.endpoint, keys=PushKeys.model_validate(r.keys))
                for r in rows
            ]

    async def subscribe(self, subscription: PushSubscription) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO push_subscriptions (endpoint, keys) "
                    "VALUES (:endpoint, CAST(:keys AS jsonb)) "
                    "ON CONFLICT (endpoint) DO UPDATE SET keys = EXCLUDED.keys"
                ),
                {"endpoint": subscription.endpoint, "keys": subscription.keys.model_dump_json()},
            )

    async def forget(self, endpoint: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM push_subscriptions WHERE endpoint = :endpoint"),
                {"endpoint": endpoint},
            )


def _stored(row: RowMapping) -> StoredNotification:
    data: dict[str, Any] = dict(row)
    notification = Notification.model_validate(data.pop("payload"))
    return StoredNotification.model_validate({**data, "notification": notification})
