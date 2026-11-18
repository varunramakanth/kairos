from pathlib import Path

import pytest

from kairos_domain.content import Message, ToolResultBlock, ToolUseBlock, user_text
from kairos_domain.model import ModelCompleted, ModelRequest
from kairos_domain.run import ToolCallRecord
from kairos_domain.tools import ToolContext
from kairos_harness.cassette import (
    CassetteMissError,
    RecordingProvider,
    RecordingToolExecutor,
    ReplayProvider,
    ReplayToolExecutor,
    fingerprint,
    load_cassette,
)
from kairos_harness.testing import (
    ScriptedProvider,
    StaticToolExecutor,
    simple_spec,
    text_turn,
    tool_turn,
)


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


async def test_tool_cassette_replays_results_and_the_recorded_tool_set(tmp_path: Path) -> None:
    async def search(call: ToolCallRecord, context: ToolContext) -> str:
        return f"results for {call.input['q']}"

    path = tmp_path / "tools.jsonl"
    recorder = RecordingToolExecutor(StaticToolExecutor([(simple_spec("search"), search)]), path)
    ctx = ToolContext(run_id="r", tool_call_id="t", idempotency_key="t")
    await recorder.execute(ToolCallRecord(id="t", name="search", input={"q": "evals"}), ctx)

    replay = ReplayToolExecutor(path)
    assert [s.name for s in replay.specs()] == ["search"]
    output = await replay.execute(ToolCallRecord(id="x", name="search", input={"q": "evals"}), ctx)
    assert output.content == "results for evals"
    with pytest.raises(CassetteMissError, match="no recorded result for search"):
        await replay.execute(ToolCallRecord(id="y", name="search", input={"q": "other"}), ctx)


def after_tool(result: str) -> ModelRequest:
    """A request whose last message is a tool result with the given text."""
    return ModelRequest(
        model="claude-opus-5-5",
        system="sys",
        messages=(
            user_text("schedule it"),
            Message(
                role="assistant",
                content=(ToolUseBlock(id="t1", name="schedule_create", input={"x": 1}),),
            ),
            Message(role="user", content=(ToolResultBlock(tool_use_id="t1", content=result),)),
        ),
    )


async def test_a_demo_recording_matches_whatever_the_tools_returned(tmp_path: Path) -> None:
    path = tmp_path / "demo.jsonl"
    recorder = RecordingProvider(
        ScriptedProvider([text_turn("scheduled")]), path, ignore_tool_output=True
    )
    _ = [e async for e in recorder.stream(after_tool("Proposed task job_aaa"))]
    (entry,) = load_cassette(path)
    assert entry.ignores_tool_output

    replay = ReplayProvider(path)
    events = [e async for e in replay.stream(after_tool("Proposed task job_bbb"))]
    assert [e.message.text for e in events if isinstance(e, ModelCompleted)] == ["scheduled"]
    # Anything else about the request still has to match.
    with pytest.raises(CassetteMissError):
        _ = [e async for e in ReplayProvider(path).stream(request("schedule it"))]


async def test_an_ordinary_recording_still_misses_on_a_changed_tool_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "strict.jsonl"
    recorder = RecordingProvider(ScriptedProvider([text_turn("scheduled")]), path)
    _ = [e async for e in recorder.stream(after_tool("Proposed task job_aaa"))]
    assert fingerprint(after_tool("a")) != fingerprint(after_tool("b"))
    assert fingerprint(after_tool("a"), ignore_tool_output=True) == fingerprint(
        after_tool("b"), ignore_tool_output=True
    )
    with pytest.raises(CassetteMissError):
        _ = [e async for e in ReplayProvider(path).stream(after_tool("Proposed task job_bbb"))]
