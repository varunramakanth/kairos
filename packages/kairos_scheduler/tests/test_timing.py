"""Next-run calculation, including both 2026 daylight-saving changes in Toronto.

2026-03-08: clocks go 02:00 EST -> 03:00 EDT (02:00-02:59 does not exist).
2026-11-01: clocks go 02:00 EDT -> 01:00 EST (01:00-01:59 happens twice).
"""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from kairos_domain.jobs import CronSchedule, IntervalSchedule, OneShotSchedule
from kairos_scheduler.timing import (
    ScheduleError,
    describe,
    local_text,
    next_fire,
    upcoming,
    validate,
)

TORONTO = ZoneInfo("America/Toronto")


def local(*args: int, fold: int = 0) -> datetime:
    return datetime(*args, tzinfo=TORONTO, fold=fold)  # type: ignore[misc]  # positional y/m/d/h/m


def cron(expression: str) -> CronSchedule:
    return CronSchedule(expression=expression, timezone="America/Toronto")


def test_daily_cron_keeps_local_wall_time_across_both_changes() -> None:
    schedule = cron("0 8 * * *")
    fires = upcoming(schedule, after=local(2026, 3, 6, 12, 0), count=4)
    assert [f.astimezone(TORONTO).hour for f in fires] == [8, 8, 8, 8]
    # The UTC instant moves by an hour when the offset changes.
    assert [f.hour for f in fires] == [13, 12, 12, 12]  # 8 March is already EDT at 08:00

    fires = upcoming(schedule, after=local(2026, 10, 30, 12, 0), count=3)
    assert [f.astimezone(TORONTO).hour for f in fires] == [8, 8, 8]
    assert [f.hour for f in fires] == [12, 13, 13]  # 1 November is already EST at 08:00


def test_a_time_inside_the_spring_forward_gap_runs_an_hour_later_and_is_not_skipped() -> None:
    fires = upcoming(cron("30 2 * * *"), after=local(2026, 3, 7, 12, 0), count=3)
    walls = [f.astimezone(TORONTO) for f in fires]
    assert [(w.day, w.hour, w.minute) for w in walls] == [(8, 3, 30), (9, 2, 30), (10, 2, 30)]


def test_a_time_in_the_repeated_hour_runs_once() -> None:
    fires = upcoming(cron("30 1 * * *"), after=local(2026, 10, 31, 12, 0), count=3)
    walls = [f.astimezone(TORONTO) for f in fires]
    assert [(w.day, w.hour) for w in walls] == [(1, 1), (2, 1), (3, 1)]
    assert walls[0].utcoffset() == timedelta(hours=-4)  # the first 01:30 (EDT)


def test_after_the_first_occurrence_of_the_repeated_hour_it_does_not_fire_again() -> None:
    first_0130 = local(2026, 11, 1, 1, 30, fold=0)
    second_0130 = local(2026, 11, 1, 1, 30, fold=1)
    # Same-zone subtraction ignores fold; compare instants.
    assert second_0130.astimezone(UTC) - first_0130.astimezone(UTC) == timedelta(hours=1)
    nxt = next_fire(cron("30 1 * * *"), after=first_0130)
    assert nxt is not None
    assert nxt.astimezone(TORONTO).day == 2


def test_each_local_time_fires_once_even_for_frequent_crons() -> None:
    fires = upcoming(cron("*/15 * * * *"), after=local(2026, 11, 1, 0, 50), count=10)
    walls = [f.astimezone(TORONTO).strftime("%H:%M") for f in fires]
    assert len(set(walls)) == len(walls)  # no wall-clock time repeats
    gaps = [b - a for a, b in pairwise(fires)]
    # The repeated hour is not run a second time: one 75-minute gap, the rest 15.
    assert sorted(set(gaps)) == [timedelta(minutes=15), timedelta(minutes=75)]
    assert gaps.count(timedelta(minutes=75)) == 1


def test_interval_counts_elapsed_time() -> None:
    start = local(2026, 3, 8, 1, 0)
    fires = upcoming(IntervalSchedule(every_seconds=3600), after=start, count=2)
    assert fires[0] - start == timedelta(hours=1)
    assert fires[0].astimezone(TORONTO).hour == 3  # 01:00 EST + 1h is 03:00 EDT


def test_one_shot_fires_once() -> None:
    at = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    schedule = OneShotSchedule(at=at)
    assert next_fire(schedule, after=at - timedelta(seconds=1)) == at
    assert next_fire(schedule, after=at) is None
    assert upcoming(schedule, after=at - timedelta(days=1), count=3) == [at]


def test_validation() -> None:
    now = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    validate(cron("0 8 * * mon-fri"), now=now)
    with pytest.raises(ScheduleError, match="five-field"):
        validate(cron("0 8 * *"), now=now)
    with pytest.raises(ScheduleError, match="five-field"):
        validate(cron("whenever"), now=now)
    with pytest.raises(ScheduleError, match="timezone"):
        validate(CronSchedule(expression="0 8 * * *", timezone="Mars/Olympus"), now=now)
    with pytest.raises(ScheduleError, match="past"):
        validate(OneShotSchedule(at=now - timedelta(minutes=1)), now=now)
    with pytest.raises(ValueError, match="aware"):
        next_fire(cron("0 8 * * *"), after=datetime(2026, 1, 1))


def test_descriptions() -> None:
    assert describe(cron("0 8 * * *")) == "cron '0 8 * * *' (America/Toronto)"
    assert describe(IntervalSchedule(every_seconds=6 * 3600)) == "every 6h"
    assert describe(IntervalSchedule(every_seconds=5400)) == "every 1h 30m"
    moment = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    assert local_text(moment, "America/Toronto") == "Wed 18 Nov 2026, 08:00 EST"
