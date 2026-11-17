"""Deciding what a watch observation means. Pure functions, no I/O.

The model only *observes* (through ``record_observation``). Whether that is worth
an alert is decided here, deterministically, so it can be tested and cannot be
talked into by a web page:

- **Condition:** below / above a threshold, text contains, or value changed.
- **Dedupe:** an alert carries a key derived from what triggered it. The same
  key as the previous alert never alerts again, so a fare that stays at $612 is
  reported once, while a further drop to $580 is new.
- **Cooldown:** no two alerts closer together than ``cooldown_seconds``.
- **Backoff:** failed runs push the next run out exponentially.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError

from kairos_domain.content import Message, ToolUseBlock
from kairos_domain.jobs import Above, Below, Changed, Contains, Observation, WatchSpec
from kairos_domain.scheduling import PreviousAlert

OBSERVATION_TOOL = "record_observation"
BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_MAX = timedelta(hours=6)


@dataclass(frozen=True)
class Verdict:
    matched: bool
    alert: str | None
    """The alert message, or ``None`` when no alert should be raised."""
    dedupe_key: str | None
    reason: str
    """Why: shown in the job history."""


def evaluate(
    spec: WatchSpec,
    observation: Observation,
    *,
    previous: Observation | None,
    last_alert: PreviousAlert | None,
    now: datetime,
) -> Verdict:
    matched, key, description = _check(spec, observation, previous)
    if not matched or key is None:
        return Verdict(False, None, None, description)
    if last_alert is not None and last_alert.dedupe_key == key:
        return Verdict(True, None, key, f"{description}; already alerted")
    if last_alert is not None and now - last_alert.at < timedelta(seconds=spec.cooldown_seconds):
        return Verdict(True, None, key, f"{description}; in cooldown")
    source = f" ({observation.source_url})" if observation.source_url else ""
    return Verdict(True, f"{description}{source}", key, description)


def _check(
    spec: WatchSpec, observation: Observation, previous: Observation | None
) -> tuple[bool, str | None, str]:
    condition = spec.condition
    if not observation.found:
        return False, None, f"{spec.extract}: not found"
    value, unit = observation.value, f" {observation.unit}" if observation.unit else ""
    if isinstance(condition, Below | Above):
        if value is None:
            return False, None, f"{spec.extract}: no number observed"
        hit = (
            value < condition.threshold
            if isinstance(condition, Below)
            else value > condition.threshold
        )
        word = "below" if isinstance(condition, Below) else "above"
        text = f"{spec.extract} is {value:g}{unit}, {word} {condition.threshold:g}"
        return (
            hit,
            f"{condition.kind}:{value:g}",
            text if hit else f"{spec.extract} is {value:g}{unit}",
        )
    if isinstance(condition, Contains):
        hit = condition.text.lower() in observation.text.lower()
        text = f"'{condition.text}' appeared in {spec.extract}"
        return (
            hit,
            f"contains:{condition.text.lower()}",
            text if hit else f"'{condition.text}' not seen",
        )
    assert isinstance(condition, Changed)  # noqa: S101 (exhaustive union)
    if previous is None or not previous.found:
        return False, None, f"{spec.extract}: first observation recorded"
    if value is not None and previous.value is not None:
        delta = value - previous.value
        hit = abs(delta) > 0 and abs(delta) >= condition.min_delta
        text = f"{spec.extract} changed from {previous.value:g} to {value:g}{unit}"
        return (
            hit,
            f"changed:{value:g}",
            text if hit else f"{spec.extract} unchanged at {value:g}{unit}",
        )
    hit = observation.text.strip() != previous.text.strip()
    # sha256, not hash(): Python randomizes hash() per process, and keys are stored.
    key = f"changed:{hashlib.sha256(observation.text.strip().encode()).hexdigest()[:12]}"
    return hit, key, f"{spec.extract} changed" if hit else f"{spec.extract} unchanged"


def backoff(consecutive_failures: int) -> timedelta:
    """Delay before retrying after ``consecutive_failures`` failed runs (at least 1)."""
    exponent = max(consecutive_failures - 1, 0)
    return min(BACKOFF_BASE * (2**exponent), BACKOFF_MAX)


def observation_from(messages: Sequence[Message]) -> Observation | None:
    """The last valid ``record_observation`` call in a run, if any."""
    for message in reversed(messages):
        for block in reversed(message.content):
            if isinstance(block, ToolUseBlock) and block.name == OBSERVATION_TOOL:
                try:
                    return Observation.model_validate(block.input)
                except ValidationError:
                    continue
    return None
