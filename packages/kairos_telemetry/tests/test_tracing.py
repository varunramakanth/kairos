"""Spans produced by a real loop run, captured in memory."""

import json
from collections.abc import Sequence
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from kairos_domain.content import Provenance
from kairos_domain.run import RunRequest, RunState, ToolCallRecord
from kairos_domain.tools import Allow, Deny, ToolContext, ToolDecision, ToolSpec
from kairos_domain.usage import Usage
from kairos_harness import AgentLoop, BaseHooks, HookChain, Hooks, InMemoryCheckpointer
from kairos_harness.testing import (
    ScriptedProvider,
    StaticToolExecutor,
    Turn,
    simple_spec,
    text_turn,
    tool_turn,
)
from kairos_telemetry import (
    OtlpJsonFileExporter,
    TracingHooks,
    configure_tracing,
    langfuse_headers,
    spans_to_otlp_json,
)

OPUS = "claude-opus-5-5"
USAGE = Usage(
    input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=20
)


async def search(call: ToolCallRecord, context: ToolContext) -> str:
    return "results"


async def boom(call: ToolCallRecord, context: ToolContext) -> str:
    raise RuntimeError("nope")


async def traced_run(
    turns: Sequence[Turn],
    *,
    extra_hooks: Sequence[Hooks] = (),
    capture_content: bool = False,
    exporter_path: Path | None = None,
) -> list[ReadableSpan]:
    memory = InMemorySpanExporter()
    processors = [SimpleSpanProcessor(memory)]
    if exporter_path:
        processors.append(SimpleSpanProcessor(OtlpJsonFileExporter(exporter_path)))
    provider = configure_tracing(service_version="0.1.0", processors=processors)
    tracing = TracingHooks(
        provider.get_tracer("kairos"),
        provider_name="anthropic",
        harness_version="0.1.0",
        capture_content=capture_content,
    )
    loop = AgentLoop(
        provider=ScriptedProvider(turns),
        tools=StaticToolExecutor(
            [
                (simple_spec("web_search", provenance=Provenance.UNTRUSTED), search),
                (simple_spec("boom"), boom),
            ]
        ),
        checkpointer=InMemoryCheckpointer(),
        hooks=HookChain([*extra_hooks, tracing]),
    )
    request = RunRequest(
        run_id="run_1",
        input="find fares",
        model="claude-opus-5-5",
        thread_id="thread_9",
        metadata={"job_id": "job_3"},
    )
    _ = [e async for e in loop.run(request)]
    return list(memory.get_finished_spans())


def by_name(spans: Sequence[ReadableSpan]) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans}


async def test_run_produces_agent_model_and_tool_spans_in_one_trace() -> None:
    turns = [
        tool_turn(
            ("t1", "web_search", {}), ("t2", "boom", {}), usage=USAGE, model="claude-opus-5-5"
        ),
        text_turn("Cheapest is $1,250.", usage=USAGE, model=OPUS),
    ]
    spans = await traced_run(turns)

    names = sorted(span.name for span in spans)
    assert names == [
        "chat claude-opus-5-5",
        "chat claude-opus-5-5",
        "execute_tool boom",
        "execute_tool web_search",
        "invoke_agent kairos",
    ]
    root = by_name(spans)["invoke_agent kairos"]
    assert root.parent is None
    assert root.kind is SpanKind.INTERNAL
    assert root.context is not None
    for span in spans:
        assert span.context is not None
        assert span.context.trace_id == root.context.trace_id
        if span is not root:
            assert span.parent is not None
            assert span.parent.span_id == root.context.span_id
        # Every span can be sliced by run, thread, job and harness version.
        assert span.attributes is not None
        assert span.attributes["kairos.run_id"] == "run_1"
        assert span.attributes["kairos.thread_id"] == "thread_9"
        assert span.attributes["kairos.job_id"] == "job_3"
        assert span.attributes["kairos.harness_version"] == "0.1.0"

    assert root.attributes is not None
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.agent.name"] == "kairos"
    assert root.attributes["gen_ai.conversation.id"] == "thread_9"
    assert root.attributes["kairos.stop_reason"] == "completed"
    assert root.attributes["kairos.steps"] == 2
    assert root.attributes["kairos.tool_calls"] == 2
    assert root.attributes["gen_ai.usage.output_tokens"] == 10
    # $4 / $20 per MTok, cache read $0.20, cache write $5: two identical calls.
    expected_cost = 2 * (10 * 4 + 5 * 20 + 100 * 0.20 + 20 * 5) / 1_000_000
    assert abs(float(str(root.attributes["kairos.cost_usd"])) - expected_cost) < 1e-12


async def test_model_span_follows_genai_conventions() -> None:
    spans = await traced_run([text_turn("hi", usage=USAGE, model="claude-opus-5-5")])
    chat = by_name(spans)["chat claude-opus-5-5"]

    assert chat.kind is SpanKind.CLIENT
    assert dict(chat.attributes or {}) | {"gen_ai.usage.cost": 0} == {
        "kairos.run_id": "run_1",
        "kairos.thread_id": "thread_9",
        "kairos.job_id": "job_3",
        "kairos.harness_version": "0.1.0",
        "kairos.step": 1,
        "langfuse.session.id": "thread_9",
        "gen_ai.agent.name": "kairos",
        "gen_ai.conversation.id": "thread_9",
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-opus-5-5",
        "gen_ai.request.max_tokens": 32_000,
        "gen_ai.response.model": "claude-opus-5-5",
        "gen_ai.response.finish_reasons": ("end_turn",),
        # Input tokens include cached ones; the cache counts are subsets.
        "gen_ai.usage.input_tokens": 130,
        "gen_ai.usage.output_tokens": 5,
        "gen_ai.usage.cache_read.input_tokens": 100,
        "gen_ai.usage.cache_creation.input_tokens": 20,
        "gen_ai.usage.cost": 0,
    }


async def test_tool_spans_record_risk_provenance_and_errors() -> None:
    spans = by_name(
        await traced_run(
            [tool_turn(("t1", "web_search", {}), ("t2", "boom", {})), text_turn("done")]
        )
    )
    ok, failed = spans["execute_tool web_search"], spans["execute_tool boom"]
    assert ok.attributes is not None
    assert failed.attributes is not None

    assert ok.attributes["gen_ai.operation.name"] == "execute_tool"
    assert ok.attributes["gen_ai.tool.name"] == "web_search"
    assert ok.attributes["gen_ai.tool.call.id"] == "t1"
    assert ok.attributes["kairos.tool.provenance"] == "untrusted"
    assert ok.attributes["kairos.tool.risk"] == "read"
    assert ok.status.status_code is StatusCode.UNSET

    assert failed.attributes["kairos.tool.is_error"] is True
    assert failed.attributes["error.type"] == "tool_error"
    assert failed.status.status_code is StatusCode.ERROR


async def test_denied_tools_get_no_span_and_content_is_off_by_default() -> None:
    class DenyAll(BaseHooks):
        async def before_tool(
            self, state: RunState, call: ToolCallRecord, spec: ToolSpec
        ) -> ToolDecision:
            return Deny(reason="no")

    turns = [tool_turn(("t1", "web_search", {})), text_turn("secret answer")]
    spans = await traced_run(turns, extra_hooks=[DenyAll()])

    assert not [s for s in spans if s.name.startswith("execute_tool")]
    for span in spans:
        assert not {"input.value", "output.value"} & set(span.attributes or {})

    captured = by_name(await traced_run([text_turn("secret answer")], capture_content=True))
    root = captured["invoke_agent kairos"]
    assert root.attributes is not None
    assert root.attributes["input.value"] == "find fares"
    assert root.attributes["output.value"] == "secret answer"


async def test_content_capture_records_requested_and_executed_tool_arguments() -> None:
    class DenyBoom(BaseHooks):
        async def before_tool(
            self, state: RunState, call: ToolCallRecord, spec: ToolSpec
        ) -> ToolDecision:
            return Deny(reason="no") if call.name == "boom" else Allow()

    turns = [
        tool_turn(("t1", "web_search", {"query": "fares"}), ("t2", "boom", {"x": 1})),
        text_turn("done"),
    ]
    spans = await traced_run(turns, extra_hooks=[DenyBoom()], capture_content=True)
    first_chat = next(s for s in spans if s.name.startswith("chat") and s.attributes)
    assert first_chat.attributes is not None
    # Evals see what the model asked for, including the call policy then blocked.
    assert json.loads(str(first_chat.attributes["kairos.requested_tool_calls"])) == [
        {"id": "t1", "name": "web_search", "arguments": {"query": "fares"}},
        {"id": "t2", "name": "boom", "arguments": {"x": 1}},
    ]
    executed = by_name(spans)["execute_tool web_search"]
    assert executed.attributes is not None
    assert json.loads(str(executed.attributes["gen_ai.tool.call.arguments"])) == {"query": "fares"}


async def test_otlp_json_export_uses_hex_ids(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    spans = await traced_run([text_turn("hi")], exporter_path=path)

    document = spans_to_otlp_json(spans)
    (resource_spans,) = document["resourceSpans"]
    resource = {a["key"]: a["value"] for a in resource_spans["resource"]["attributes"]}
    assert resource["service.name"] == {"stringValue": "kairos"}
    exported = resource_spans["scopeSpans"][0]["spans"]
    root = by_name(spans)["invoke_agent kairos"]
    assert root.context is not None
    assert {s["traceId"] for s in exported} == {f"{root.context.trace_id:032x}"}
    assert len(path.read_text().splitlines()) == len(spans)  # one export per finished span


def test_langfuse_headers_use_basic_auth() -> None:
    assert langfuse_headers("pk-lf-kairos-local", "sk-lf-kairos-local") == {
        "Authorization": "Basic cGstbGYta2Fpcm9zLWxvY2FsOnNrLWxmLWthaXJvcy1sb2NhbA==",
        "x-langfuse-ingestion-version": "4",
    }
