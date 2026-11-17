from datetime import UTC, datetime, timedelta

from kairos_domain.content import Message, TextBlock, ToolUseBlock
from kairos_domain.jobs import Below, Changed, Contains, Observation, WatchSpec
from kairos_domain.scheduling import PreviousAlert
from kairos_scheduler.watch import (
    OBSERVATION_TOOL,
    backoff,
    evaluate,
    observation_from,
)

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
FARE = WatchSpec(
    target="YYZ to CJB round trip, Dec 10-20",
    extract="lowest fare",
    condition=Below(threshold=1500),
    cooldown_seconds=3600,
)


def fare(value: float | None, *, found: bool = True) -> Observation:
    return Observation(found=found, value=value, unit="CAD", source_url="https://fares.example")


def test_below_threshold_alerts_once_per_price() -> None:
    first = evaluate(FARE, fare(1420), previous=None, last_alert=None, now=NOW)
    assert first.matched
    assert first.alert == "lowest fare is 1420 CAD, below 1500 (https://fares.example)"
    assert first.dedupe_key == "below:1420"

    same = evaluate(
        FARE,
        fare(1420),
        previous=fare(1420),
        last_alert=PreviousAlert("below:1420", NOW),
        now=NOW + timedelta(hours=6),
    )
    assert same.matched
    assert same.alert is None
    assert "already alerted" in same.reason

    lower = evaluate(
        FARE,
        fare(1380),
        previous=fare(1420),
        last_alert=PreviousAlert("below:1420", NOW),
        now=NOW + timedelta(hours=6),
    )
    assert lower.alert is not None


def test_cooldown_holds_back_a_new_alert() -> None:
    verdict = evaluate(
        FARE,
        fare(1380),
        previous=fare(1420),
        last_alert=PreviousAlert("below:1420", NOW),
        now=NOW + timedelta(minutes=30),
    )
    assert verdict.matched
    assert verdict.alert is None
    assert "cooldown" in verdict.reason


def test_no_alert_above_threshold_or_when_not_found() -> None:
    assert not evaluate(FARE, fare(1610), previous=None, last_alert=None, now=NOW).matched
    missing = evaluate(FARE, fare(None, found=False), previous=None, last_alert=None, now=NOW)
    assert not missing.matched
    assert missing.reason == "lowest fare: not found"
    no_number = evaluate(FARE, fare(None), previous=None, last_alert=None, now=NOW)
    assert no_number.reason == "lowest fare: no number observed"


def test_contains_is_case_insensitive() -> None:
    spec = WatchSpec(
        target="https://shop.example/x", extract="stock status", condition=Contains(text="In stock")
    )
    seen = Observation(found=True, text="Now IN STOCK - ships today")
    assert evaluate(spec, seen, previous=None, last_alert=None, now=NOW).alert is not None


def test_changed_needs_a_previous_observation_and_a_big_enough_move() -> None:
    spec = WatchSpec(target="t", extract="price", condition=Changed(min_delta=10))
    assert not evaluate(spec, fare(100), previous=None, last_alert=None, now=NOW).matched
    assert not evaluate(spec, fare(105), previous=fare(100), last_alert=None, now=NOW).matched
    moved = evaluate(spec, fare(85), previous=fare(100), last_alert=None, now=NOW)
    assert moved.alert is not None
    assert "from 100 to 85" in moved.alert


def test_backoff_doubles_and_caps() -> None:
    assert [backoff(n) for n in (1, 2, 3)] == [
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=20),
    ]
    assert backoff(20) == timedelta(hours=6)


def test_observation_is_read_from_the_last_valid_tool_call() -> None:
    messages = [
        Message(
            role="assistant",
            content=(
                ToolUseBlock(id="a", name=OBSERVATION_TOOL, input={"found": True, "value": 1}),
            ),
        ),
        Message(role="user", content=(TextBlock(text="ok"),)),
        Message(
            role="assistant",
            content=(
                ToolUseBlock(id="b", name=OBSERVATION_TOOL, input={"found": True, "value": 2}),
                ToolUseBlock(id="c", name=OBSERVATION_TOOL, input={"value": "not valid"}),
            ),
        ),
    ]
    observation = observation_from(messages)
    assert observation is not None
    assert observation.value == 2
    assert observation_from([]) is None
