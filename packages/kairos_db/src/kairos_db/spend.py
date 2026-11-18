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

    async def spent_today(self, now: datetime) -> float:
        start = local_midnight(now, self._timezone)
        async with self._engine.connect() as connection:
            value = (
                await connection.execute(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM run_steps "
                        "WHERE created_at >= :start AND created_at < :end"
                    ),
                    {"start": start, "end": start + timedelta(days=1, hours=1)},
                )
            ).scalar_one()
        return float(value)

    def resets_at(self, now: datetime) -> datetime:
        """The next local midnight (DST-safe: a calendar day, not 24 hours)."""
        tomorrow = local_midnight(now, self._timezone).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time(0), tzinfo=ZoneInfo(self._timezone))
