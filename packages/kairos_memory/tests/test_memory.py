"""Memory tools, proposals and the reflection hook (no database, scripted model)."""

from datetime import UTC, datetime

from kairos_domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus
from kairos_domain.run import RunRequest, RunState, StopReason
from kairos_domain.tools import Allow, RequireApproval, ToolContext
from kairos_harness.testing import ScriptedProvider, text_turn
from kairos_memory.reflection import ReflectionHooks, parse
from kairos_memory.testing import MemoryMemories
from kairos_memory.tools import memory_tools
from kairos_policy import Policy

NOW = datetime(2026, 11, 18, tzinfo=UTC)


def memory(id: str, content: str, status: MemoryStatus = MemoryStatus.ACTIVE) -> Memory:
    return Memory(
        id=id,
        kind=MemoryKind.PREFERENCE,
        content=content,
        status=status,
        source=MemorySource.USER,
        run_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def ctx() -> ToolContext:
    return ToolContext(run_id="run_1", tool_call_id="t1", idempotency_key="t1")


async def test_recall_returns_only_confirmed_memories() -> None:
    store = MemoryMemories(
        [
            memory("mem_1", "Prefers aisle seats on flights"),
            memory("mem_2", "Prefers window seats on flights", MemoryStatus.PROPOSED),
        ]
    )
    recall, _, _ = memory_tools(store)
    out = await recall({"query": "seats flights"}, ctx())
    assert out.content == "- [mem_1] (preference) Prefers aisle seats on flights"
    assert (await recall({"query": "hotels"}, ctx())).content == "No memories match 'hotels'."


async def test_remember_proposes_once_and_forget_deletes() -> None:
    store = MemoryMemories()
    _, remember, forget = memory_tools(store)
    first = await remember({"content": "Lives in Toronto", "kind": "profile"}, ctx())
    assert first.content.startswith("Proposed memory mem_0001")
    (saved,) = store.items.values()
    assert (saved.status, saved.source, saved.run_id) == (
        MemoryStatus.PROPOSED,
        MemorySource.AGENT,
        "run_1",
    )
    again = await remember({"content": "  lives in   toronto ", "kind": "profile"}, ctx())
    assert again.content.startswith("Already remembered")
    assert (await forget({"memory_id": "mem_0001"}, ctx())).content == "Forgot mem_0001."
    assert (await forget({"memory_id": "mem_0001"}, ctx())).is_error


def test_policy_lets_the_agent_propose_but_not_delete() -> None:
    store = MemoryMemories()
    _, remember, forget = memory_tools(store)
    policy = Policy()
    assert isinstance(policy.decide(remember.spec, actions_enabled=True), Allow)
    assert isinstance(policy.decide(forget.spec, actions_enabled=True), RequireApproval)


def finished_state(
    *,
    thread: str | None = "th_1",
    stop: StopReason = StopReason.COMPLETED,
    metadata: dict[str, str] | None = None,
) -> RunState:
    request = RunRequest(
        run_id="run_9",
        input="I'm vegetarian, find me a dinner spot near Union Station",
        model="claude-sonnet-5-5",
        thread_id=thread,
        metadata=metadata or {},
    )
    state = RunState(request=request)
    state.stop_reason = stop
    state.output_text = "Here are three vegetarian-friendly places near Union Station…"
    return state


async def test_reflection_proposes_durable_facts_from_the_exchange_only() -> None:
    provider = ScriptedProvider(
        [
            text_turn(
                'Sure: {"memories": [{"kind": "preference", "content": "Is vegetarian"},'
                ' {"kind": "bogus", "content": "x"}]}'
            )
        ]
    )
    store = MemoryMemories()
    await ReflectionHooks(provider, store, model="claude-haiku-5-5").on_complete(finished_state())
    # One malformed entry makes the reply invalid as a whole: nothing half-parsed is saved.
    assert store.items == {}

    provider = ScriptedProvider(
        [text_turn('{"memories": [{"kind": "preference", "content": "Is vegetarian"}]}')]
    )
    await ReflectionHooks(provider, store, model="claude-haiku-5-5").on_complete(finished_state())
    (saved,) = store.items.values()
    assert (saved.content, saved.status, saved.source) == (
        "Is vegetarian",
        MemoryStatus.PROPOSED,
        MemorySource.REFLECTION,
    )
    (request,) = provider.requests
    assert request.model == "claude-haiku-5-5"
    assert request.tools == ()
    assert "vegetarian" in str(request.messages[0].content)


async def test_reflection_skips_jobs_subagents_pauses_and_survives_failures() -> None:
    store = MemoryMemories()
    for state in (
        finished_state(thread=None),
        finished_state(stop=StopReason.AWAITING_APPROVAL),
        finished_state(metadata={"job_id": "job_1"}),
        finished_state(metadata={"subagent": "research"}),
    ):
        provider = ScriptedProvider([])
        await ReflectionHooks(provider, store, model="m").on_complete(state)
        assert provider.requests == []
    broken = ScriptedProvider([RuntimeError("provider down")])
    await ReflectionHooks(broken, store, model="m").on_complete(finished_state())  # no raise
    assert store.items == {}


def test_parse_is_tolerant_of_prose_and_caps_proposals() -> None:
    many = ", ".join(f'{{"kind": "task", "content": "goal {i}"}}' for i in range(5))
    assert len(parse(f'Here you go:\n{{"memories": [{many}]}}\nDone.')) == 3
    assert parse("nothing to remember") == []
    assert parse('{"memories": []}') == []
