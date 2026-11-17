"""When a schedule fires next. Pure functions; time is always passed in.

Cron expressions are evaluated on the local wall clock of the schedule's timezone,
which is what a person means by "every day at 8am". Daylight saving needs two rules:

- **Spring forward** (a local time that does not exist, e.g. 02:30 on the March
  change in Toronto): the run happens at the same offset from midnight in the new
  offset, i.e. one hour later on the wall clock (03:30). It is never skipped.
- **Fall back** (a local time that happens twice, e.g. 01:30 on the November
  change): the run happens once, at the first occurrence. This applies to every
  expression, so a ``*/15`` cron also skips the repeated hour (one 75-minute gap)
  rather than running the same wall-clock times twice. Use an interval schedule
  when elapsed time is what matters.

Intervals count elapsed time and ignore the wall clock entirely.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from kairos_domain.jobs import CronSchedule, IntervalSchedule, OneShotSchedule, Schedule


class ScheduleError(ValueError):
    """A schedule that cannot be used (bad cron, unknown timezone, time in the past)."""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError(f"unknown timezone: {name!r}") from error


def validate(schedule: Schedule, *, now: datetime) -> None:
    """Raise ``ScheduleError`` unless the schedule can fire at least once after ``now``."""
    if isinstance(schedule, CronSchedule):
        zone(schedule.timezone)
        if not croniter.is_valid(schedule.expression) or len(schedule.expression.split()) != 5:
            raise ScheduleError(f"not a five-field cron expression: {schedule.expression!r}")
    elif isinstance(schedule, OneShotSchedule) and schedule.at <= now:
        raise ScheduleError(f"{schedule.at.isoformat()} is in the past")


def next_fire(schedule: Schedule, *, after: datetime) -> datetime | None:
    """The first firing strictly after ``after`` (an aware datetime), in UTC.

    ``None`` means the schedule will never fire again (a one-shot in the past).
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    if isinstance(schedule, IntervalSchedule):
        return (after + timedelta(seconds=schedule.every_seconds)).astimezone(UTC)
    if isinstance(schedule, OneShotSchedule):
        return schedule.at.astimezone(UTC) if schedule.at > after else None
    return _next_cron(schedule, after)


def _next_cron(schedule: CronSchedule, after: datetime) -> datetime:
    tz = zone(schedule.timezone)
    # Iterate on naive local wall-clock times, then attach the zone. Starting from
    # the local wall time of `after` (whichever fold it is in) means an ambiguous
    # hour that already fired is not matched again.
    local_after = after.astimezone(tz).replace(tzinfo=None)
    iterator = croniter(schedule.expression, local_after)
    while True:
        wall: datetime = iterator.get_next(datetime)
        candidate = _resolve(wall, tz)
        if candidate > after:
            return candidate.astimezone(UTC)


def _resolve(wall: datetime, tz: ZoneInfo) -> datetime:
    """Attach ``tz`` to a naive wall time, applying the DST rules in the module doc."""
    first = wall.replace(tzinfo=tz, fold=0)
    # A time in the spring-forward gap does not survive a round trip through UTC.
    # fold=0 uses the offset from before the gap, so the instant is one hour after
    # the intended wall time: 02:30 EST is 03:30 EDT.
    round_trip = first.astimezone(UTC).astimezone(tz)
    if round_trip.replace(tzinfo=None) != wall:
        return round_trip
    return first


def upcoming(schedule: Schedule, *, after: datetime, count: int = 3) -> list[datetime]:
    """The next ``count`` firings, for confirming a schedule back to the user."""
    times: list[datetime] = []
    cursor = after
    while len(times) < count:
        fire = next_fire(schedule, after=cursor)
        if fire is None:
            break
        times.append(fire)
        cursor = fire
    return times


def describe(schedule: Schedule) -> str:
    """A short human description, e.g. ``cron '0 8 * * mon-fri' (America/Toronto)``."""
    if isinstance(schedule, CronSchedule):
        return f"cron '{schedule.expression}' ({schedule.timezone})"
    if isinstance(schedule, IntervalSchedule):
        hours, rest = divmod(schedule.every_seconds, 3600)
        minutes = rest // 60
        parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if minutes else [])
        return f"every {' '.join(parts) or f'{schedule.every_seconds}s'}"
    return f"once at {schedule.at.isoformat()}"


def local_text(moment: datetime, timezone: str) -> str:
    """``Tue 18 Nov 2026, 08:00 EST``: how a time is shown back to the user."""
    return moment.astimezone(zone(timezone)).strftime("%a %d %b %Y, %H:%M %Z")
