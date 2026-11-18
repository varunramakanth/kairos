"""The daily spend cap (ADR-015): a hook that stops runs once today's spend reaches the
cap, and tells the owner once a day."""

from datetime import datetime

import structlog

from kairos_db.governance import NotificationStore
from kairos_db.spend import SpendLedger
from kairos_domain.governance import Notification, NotificationKind
from kairos_domain.model import ModelRequest
from kairos_domain.run import RunState
from kairos_harness import BaseHooks, BudgetStop, Clock

log = structlog.get_logger(__name__)


class SpendCap:
    """Today's spend against the cap. Shared by the hook and the API."""

    def __init__(self, ledger: SpendLedger, *, cap_usd: float | None, clock: Clock) -> None:
        self.ledger = ledger
        self.cap_usd = cap_usd
        self._clock = clock

    async def blocked(self) -> str | None:
        """Why new work must stop now, or ``None`` when under the cap."""
        if self.cap_usd is None:
            return None
        now = self._clock.now()
        spent = await self.ledger.spent_today(now)
        if spent < self.cap_usd:
            return None
        resets = self.ledger.resets_at(now)
        return (
            f"daily spend cap of ${self.cap_usd:.2f} reached (${spent:.2f} spent today); "
            f"runs resume after {resets:%H:%M %Z} on {resets:%a %d %b}"
        )


class SpendCapHooks(BaseHooks):
    """Checks the cap before every model call, so a runaway run or job stops mid-way."""

    def __init__(self, cap: SpendCap, notifications: NotificationStore, *, clock: Clock) -> None:
        self._cap = cap
        self._notifications = notifications
        self._clock = clock

    async def before_model(self, state: RunState, request: ModelRequest) -> ModelRequest:
        reason = await self._cap.blocked()
        if reason is None:
            return request
        await self._notify_once(reason, self._clock.now())
        raise BudgetStop(reason)

    async def _notify_once(self, reason: str, now: datetime) -> None:
        day = now.astimezone(self._cap.ledger.resets_at(now).tzinfo).date().isoformat()
        try:
            await self._notifications.enqueue(
                Notification(
                    kind=NotificationKind.MESSAGE,
                    title="Kairos stopped: daily spend cap reached",
                    body=f"{reason[0].upper()}{reason[1:]}. Raise the cap in .env "
                    "(KAIROS_DAILY_SPEND_CAP_USD) if this was expected.",
                    url="/settings",
                ),
                dedupe_key=f"spend-cap:{day}",
            )
        except Exception as error:  # the stop matters more than the notice
            log.warning("spend_cap_notice_failed", error=str(error))
