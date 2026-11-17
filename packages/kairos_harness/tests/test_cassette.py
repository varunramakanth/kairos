from pathlib import Path

import pytest

from kairos_domain.content import user_text
from kairos_domain.model import ModelCompleted, ModelRequest
from kairos_harness.cassette import (
    CassetteMissError,
    RecordingProvider,
    ReplayProvider,
    fingerprint,
    load_cassette,
)
from kairos_harness.testing import ScriptedProvider, text_turn, tool_turn


def request(text: str = "hi") -> ModelRequest:
    return ModelRequest(model="claude-opus-5-5", system="sys", messages=(user_text(text),))


async def test_replay_returns_exactly_what_was_recorded(tmp_path: Path) -> None:
    path = tmp_path / "cassette.jsonl"
    turns = [tool_turn(("t1", "echo", {"q": "a"}), text="let me check"), text_turn("done")]
    recorder = RecordingProvider(ScriptedProvider(turns, name="anthropic"), path)
    recorded = [
        [e async for e in recorder.stream(request("one"))],
        [e async for e in recorder.stream(request("two"))],
    ]

    replay = ReplayProvider(path, name="anthropic")
    assert replay.name == "anthropic"
    # Order of lookup does not matter; matching is by request fingerprint.
    assert [e async for e in replay.stream(request("two"))] == recorded[1]
    assert [e async for e in replay.stream(request("one"))] == recorded[0]
    assert [e.summary for e in load_cassette(path)] == [
        "claude-opus-5-5 | 1 messages | last 'one'",
        "claude-opus-5-5 | 1 messages | last 'two'",
    ]


async def test_identical_requests_are_served_in_recorded_order(tmp_path: Path) -> None:
    path = tmp_path / "cassette.jsonl"
    recorder = RecordingProvider(ScriptedProvider([text_turn("first"), text_turn("second")]), path)
    for _ in range(2):
        _ = [e async for e in recorder.stream(request())]

    replay = ReplayProvider(path)
    last = [[e async for e in replay.stream(request())][-1] for _ in range(2)]
    assert [e.message.text for e in last if isinstance(e, ModelCompleted)] == ["first", "second"]


async def test_a_miss_fails_loudly_and_lists_what_was_recorded(tmp_path: Path) -> None:
    path = tmp_path / "cassette.jsonl"
    recorder = RecordingProvider(ScriptedProvider([text_turn("x")]), path)
    _ = [e async for e in recorder.stream(request("recorded"))]

    replay = ReplayProvider(path)
    with pytest.raises(CassetteMissError) as error:
        _ = [e async for e in replay.stream(request("changed prompt"))]
    assert "last 'changed prompt'" in str(error.value)
    assert "last 'recorded'" in str(error.value)


def test_fingerprint_depends_on_everything_the_answer_depends_on() -> None:
    base = request()
    assert fingerprint(base) == fingerprint(request())
    assert fingerprint(base) != fingerprint(base.model_copy(update={"system": "other"}))
    assert fingerprint(base) != fingerprint(base.model_copy(update={"model": "claude-haiku-5-5"}))
    assert fingerprint(base) != fingerprint(base.model_copy(update={"effort": "low"}))


async def test_call_is_recorded_even_if_the_consumer_stops_at_the_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cassette.jsonl"
    recorder = RecordingProvider(ScriptedProvider([text_turn("x")]), path)
    async for event in recorder.stream(request()):
        if isinstance(event, ModelCompleted):
            break  # the agent loop does exactly this
    assert len(load_cassette(path)) == 1
