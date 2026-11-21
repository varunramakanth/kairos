"""Daily spend caps (ADR-015, ADR-018): a hook that stops runs once today's spend
reaches a cap, and tells the run's user once a day.

Two caps apply: the overall one, for everyone together, and each user's own.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

from kairos_db.governance import NotificationStore
from kairos_db.spend import SpendLedger
from kairos_domain.governance import Notification, NotificationKind
from kairos_domain.model import ModelRequest
from kairos_domain.run import RunState
from kairos_harness import BaseHooks, BudgetStop, Clock

log = structlog.get_logger(__name__)

type UserCap = Callable[[str], Awaitable[float | None]]
"""A user's own daily cap, or ``None`` for none."""


class SpendCap:
    """Today's spend against the caps. Shared by the hook and the API."""

    def __init__(
        self,
        ledger: SpendLedger,
        *,
        cap_usd: float | None,
        clock: Clock,
        user_cap: UserCap | None = None,
    ) -> None:
        self.ledger = ledger
        self.cap_usd = cap_usd
        self._clock = clock
        self._user_cap = user_cap

    async def blocked(self, *, owner: str | None) -> str | None:
        """Why new work for ``owner`` must stop now, or ``None`` when under both caps."""
        now = self._clock.now()
        resets = self.ledger.resets_at(now)
        when = f"runs resume after {resets:%H:%M %Z} on {resets:%a %d %b}"
        if self.cap_usd is not None:
            spent = await self.ledger.spent_today(now)
            if spent >= self.cap_usd:
                return (
                    f"daily spend cap of ${self.cap_usd:.2f} reached "
                    f"(${spent:.2f} spent today); {when}"
                )
        if owner is not None and self._user_cap is not None:
            cap = await self._user_cap(owner)
            if cap is not None:
                spent = await self.ledger.spent_today(now, owner=owner)
                if spent >= cap:
                    return (
                        f"your daily spend cap of ${cap:.2f} reached "
                        f"(${spent:.2f} spent today); {when}"
                    )
        return None


class SpendCapHooks(BaseHooks):
    """Checks the caps before every model call, so a runaway run or job stops mid-way."""

    def __init__(self, cap: SpendCap, notifications: NotificationStore, *, clock: Clock) -> None:
        self._cap = cap
        self._notifications = notifications
        self._clock = clock

    async def before_model(self, state: RunState, request: ModelRequest) -> ModelRequest:
        owner = state.request.user_id
        reason = await self._cap.blocked(owner=owner)
        if reason is None:
            return request
        await self._notify_once(reason, owner, self._clock.now())
        raise BudgetStop(reason)

    async def _notify_once(self, reason: str, owner: str, now: datetime) -> None:
        day = now.astimezone(self._cap.ledger.resets_at(now).tzinfo).date().isoformat()
        try:
            await self._notifications.enqueue(
                Notification(
                    kind=NotificationKind.MESSAGE,
                    title="Kairos stopped: daily spend cap reached",
                    body=f"{reason[0].upper()}{reason[1:]}. The admin can raise the cap "
                    "(KAIROS_DAILY_SPEND_CAP_USD overall, or yours on the Users page).",
                    url="/settings",
                ),
                owner=owner,
                dedupe_key=f"spend-cap:{owner}:{day}",
            )
        except Exception as error:  # the stop matters more than the notice
            log.warning("spend_cap_notice_failed", error=str(error))
