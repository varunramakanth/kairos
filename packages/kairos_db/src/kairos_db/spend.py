"""Daily spend, summed from ``run_steps`` (one row per model call, with its cost).

"Today" is the owner's local day, so a $5 cap resets at local midnight. Calls made
outside runs (injection screening, reflection) are small and not counted.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def local_midnight(now: datetime, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time(0), tzinfo=tz)


class SpendLedger:
    def __init__(self, engine: AsyncEngine, *, timezone: str) -> None:
        self._engine = engine
        self._timezone = timezone

    async def spent_today(self, now: datetime, *, owner: str | None = None) -> float:
        """Today's model spend: one user's with ``owner``, everyone's without."""
        start = local_midnight(now, self._timezone)
        async with self._engine.connect() as connection:
            value = (
                await connection.execute(
                    text(
                        "SELECT COALESCE(SUM(s.cost_usd), 0) FROM run_steps s "
                        "JOIN runs r ON r.id = s.run_id "
                        "WHERE s.created_at >= :start AND s.created_at < :end "
                        "AND (CAST(:owner AS text) IS NULL OR r.user_id = :owner)"
                    ),
                    {"start": start, "end": start + timedelta(days=1, hours=1), "owner": owner},
                )
            ).scalar_one()
        return float(value)

    def resets_at(self, now: datetime) -> datetime:
        """The next local midnight (DST-safe: a calendar day, not 24 hours)."""
        tomorrow = local_midnight(now, self._timezone).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time(0), tzinfo=ZoneInfo(self._timezone))
