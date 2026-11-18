"""Kairos-specific: natural language to schedule. Parse accuracy, timezone and DST.

The firing times are computed here independently of the agent's scheduler, so the
metric is an oracle rather than a copy of the code under test. The DST rules match
ADR-009: a wall time in the spring-forward gap fires an hour later, and a repeated
wall time in the fall-back hour fires once.
"""

from datetime import UTC, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import JsonValue

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score, not_applicable

DEFAULT_TIMEZONE = "America/Toronto"
FIRINGS = 6
"""How many upcoming firings two cron schedules must agree on."""


def _resolve(wall: datetime, tz: ZoneInfo) -> datetime:
    first = wall.replace(tzinfo=tz, fold=0)
    round_trip = first.astimezone(UTC).astimezone(tz)
    return round_trip if round_trip.replace(tzinfo=None) != wall else first


def cron_firings(
    expression: str, timezone: str, *, after: datetime, count: int = FIRINGS
) -> list[datetime]:
    """The next ``count`` firings of a five-field cron in ``timezone``, in UTC."""
    tz = ZoneInfo(timezone)
    iterator = croniter(expression, after.astimezone(tz).replace(tzinfo=None))
    firings: list[datetime] = []
    while len(firings) < count:
        candidate = _resolve(iterator.get_next(datetime), tz)
        if candidate > after and (not firings or candidate > firings[-1]):
            firings.append(candidate.astimezone(UTC))
    return firings


def _instant(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def same_schedule(
    expected: dict[str, JsonValue], actual: dict[str, JsonValue], *, now: datetime
) -> tuple[bool, str]:
    """Whether two schedules fire at the same times from ``now`` on, with a reason.

    Cron schedules are compared by their next firings in their own timezones, so
    equivalent expressions match. An interval matches a cron schedule whose gaps
    all equal the interval ("every 6 hours" either way). One-shot times must agree
    to the minute.
    """
    e_kind, a_kind = expected.get("kind"), actual.get("kind")
    try:
        if e_kind == a_kind == "one_shot":
            e_at, a_at = _instant(expected.get("at")), _instant(actual.get("at"))
            if a_at is None:
                return False, f"one-shot time {actual.get('at')!r} has no timezone offset"
            ok = e_at is not None and abs((e_at - a_at).total_seconds()) < 60
            return ok, f"at {a_at.isoformat()} vs expected {expected.get('at')}"
        if e_kind == a_kind == "interval":
            ok = expected.get("every_seconds") == actual.get("every_seconds")
            return ok, f"every {actual.get('every_seconds')}s vs {expected.get('every_seconds')}s"
        if e_kind == a_kind == "cron":
            e = cron_firings(str(expected["expression"]), _tz(expected), after=now)
            a = cron_firings(str(actual["expression"]), _tz(actual), after=now)
            if e == a:
                return True, f"{actual['expression']} ({_tz(actual)}) fires as expected"
            first = next(i for i, (x, y) in enumerate(zip(e, a, strict=True)) if x != y)
            return False, (
                f"{actual['expression']} ({_tz(actual)}) firing {first + 1} is "
                f"{a[first].isoformat()}, expected {e[first].isoformat()}"
            )
        if (e_kind, a_kind) in (("interval", "cron"), ("cron", "interval")):
            cron, interval = (expected, actual) if e_kind == "cron" else (actual, expected)
            firings = cron_firings(str(cron["expression"]), _tz(cron), after=now)
            gaps = {(b - a).total_seconds() for a, b in pairwise(firings)}
            ok = gaps == {float(str(interval.get("every_seconds")))}
            return ok, f"cron gaps {sorted(gaps)}s vs interval {interval.get('every_seconds')}s"
    except (KeyError, ValueError) as error:
        return False, f"unusable schedule {actual}: {error}"
    return False, f"schedule kind {a_kind} vs expected {e_kind}"


def _tz(schedule: dict[str, JsonValue]) -> str:
    value = schedule.get("timezone")
    return value if isinstance(value, str) and value else DEFAULT_TIMEZONE


class ScheduleCorrectness:
    """The proposed job has the right kind, fires at the right times and, for a
    watch, alerts on the right condition.

    Reads the last ``schedule_create`` call. The score is the share of checks
    passed (kind, then schedule, condition and expiry when expected); it passes
    when all do.
    """

    id = "schedule_correctness"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    def __init__(
        self, tool: str = "schedule_create", *, default_now: datetime | None = None
    ) -> None:
        self._tool = tool
        self._default_now = default_now

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        if example is None or example.expected_schedule is None:
            return not_applicable("no expected schedule")
        expected = example.expected_schedule
        calls = [r for r in trace.requested_tool_calls if r.name == self._tool]
        if not calls:
            return Score(value=0.0, passed=False, reason=f"no {self._tool} call")
        args = calls[-1].arguments
        now = example.now or self._default_now or _start(trace)
        checks: list[tuple[bool, str]] = []

        kind = args.get("kind")
        checks.append((kind == expected.job_kind, f"kind {kind} vs {expected.job_kind}"))

        schedule = args.get("schedule")
        if not isinstance(schedule, dict):
            checks.append((False, "no schedule in the call"))
        elif expected.schedule is not None:
            outcomes = [
                same_schedule(option, schedule, now=now)
                for option in (expected.schedule, *expected.also_accept)
            ]
            checks.append(next((o for o in outcomes if o[0]), outcomes[0]))

        if expected.expires_at is not None:
            expires = _instant(args.get("expires_at"))
            ok = (
                expires is not None
                and abs((expires - expected.expires_at).total_seconds()) <= 86_400
            )
            checks.append(
                (ok, f"expires {args.get('expires_at')} vs {expected.expires_at.isoformat()}")
            )

        if expected.condition is not None:
            watch = args.get("watch")
            condition = watch.get("condition") if isinstance(watch, dict) else None
            ok = isinstance(condition, dict) and all(
                _close(condition.get(k), v) for k, v in expected.condition.items()
            )
            checks.append((ok, f"condition {condition} vs {expected.condition}"))

        passed = [ok for ok, _ in checks]
        failures = [reason for ok, reason in checks if not ok]
        return Score(
            value=sum(passed) / len(passed),
            passed=all(passed),
            reason="; ".join(failures) or "; ".join(reason for _, reason in checks),
        )


def _close(actual: JsonValue, expected: JsonValue) -> bool:
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(actual) - float(expected)) < 1e-6
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return actual == expected


def _start(trace: Trace) -> datetime:
    """Without an example clock, the trace's own start time stands in for "now"."""
    return min((s.start for s in trace.spans), default=datetime.now(UTC))
