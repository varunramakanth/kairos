"""The eval framework on hand-written OTLP/JSON, with no Kairos code involved."""

import json
from pathlib import Path
from typing import Any

import pytest

from kairos_evals import (
    Evaluator,
    Example,
    JsonlSink,
    OtlpJsonTraceSource,
    Trace,
    TraceFilter,
    load_examples,
    summarize,
    traces_from_otlp,
)
from kairos_evals.metrics import BudgetAdherence, CitationGrounding, ToolSelectionAccuracy


def attribute(key: str, value: str | int | float) -> dict[str, Any]:
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"doubleValue": value}}


def span(
    span_id: str, name: str, parent: str | None = None, **attributes: str | int | float
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "traceId": "0" * 31 + "1",
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(1_700_000_000_000_000_000 + int(span_id, 16) * 1_000_000),
        "endTimeUnixNano": str(1_700_000_001_000_000_000 + int(span_id, 16) * 1_000_000),
        "attributes": [attribute(k.replace("__", "."), v) for k, v in attributes.items()],
    }
    if parent:
        raw["parentSpanId"] = parent
    return raw


def document(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def chat(span_id: str, tokens_in: int, tokens_out: int, cost: float) -> dict[str, Any]:
    return span(
        span_id,
        "chat some-model",
        "a1",
        gen_ai__operation__name="chat",
        gen_ai__usage__input_tokens=tokens_in,
        gen_ai__usage__output_tokens=tokens_out,
        gen_ai__usage__cost=cost,
    )


def tool(span_id: str, name: str) -> dict[str, Any]:
    return span(
        span_id,
        f"execute_tool {name}",
        "a1",
        gen_ai__operation__name="execute_tool",
        gen_ai__tool__name=name,
    )


@pytest.fixture
def trace() -> Trace:
    """An agent run from some other framework: two model calls, two tool calls."""
    root = span("a1", "invoke_agent other", gen_ai__operation__name="invoke_agent", team="blue")
    (trace,) = traces_from_otlp(
        [
            document(root, chat("b1", 1000, 50, 0.004)),
            document(
                tool("c1", "web_search"), tool("c2", "web_fetch"), chat("b2", 2000, 100, 0.008)
            ),
        ]
    )
    return trace


def test_otlp_json_is_parsed_into_a_trace(trace: Trace) -> None:
    assert trace.root is not None
    assert trace.root.name == "invoke_agent other"
    assert [s.span_id for s in trace.spans] == ["a1", "b1", "b2", "c1", "c2"]  # start order
    assert trace.tool_names == ["web_search", "web_fetch"]
    assert len(trace.model_calls) == 2
    assert trace.model_calls[0].number("gen_ai.usage.input_tokens") == 1000  # int64 as string
    assert trace.root.duration_seconds == 1.0


@pytest.mark.parametrize(
    ("expected", "value", "passed", "reason"),
    [
        (["web_fetch", "web_search"], 1.0, True, "exact match"),
        (["web_search"], 0.5, False, "unexpected ['web_fetch']"),
        (["web_search", "web_fetch", "email.send"], 2 / 3, False, "missing ['email.send']"),
        (["calendar.create"], 0.0, False, "missing ['calendar.create']; unexpected"),
        (None, 1.0, True, "no expected tools"),
    ],
)
async def test_tool_selection_accuracy(
    trace: Trace, expected: list[str] | None, value: float, passed: bool, reason: str
) -> None:
    example = Example(id="e1", input="q", expected_tools=expected)
    score = await ToolSelectionAccuracy().evaluate(trace, example)
    assert score.value == pytest.approx(value)  # pyright: ignore[reportUnknownMemberType]
    assert score.passed is passed
    assert score.reason.startswith(reason)


async def test_budget_adherence_is_computed_from_spans(trace: Trace) -> None:
    metric = BudgetAdherence()
    assert metric.usage(trace) == {
        "total_tokens": 3150,
        "cost_usd": pytest.approx(0.012),  # pyright: ignore[reportUnknownMemberType]
        "tool_calls": 2,
        "model_calls": 2,
    }

    within = Example(id="e1", input="q", budget={"total_tokens": 5000, "tool_calls": 2})
    assert (await metric.evaluate(trace, within)).passed

    over = Example(id="e2", input="q", budget={"cost_usd": 0.01, "model_calls": 1})
    score = await metric.evaluate(trace, over)
    assert not score.passed
    assert score.reason == "cost_usd 0.012 > 0.01; model_calls 2 > 1"

    with pytest.raises(ValueError, match="unknown budget keys"):
        await metric.evaluate(trace, Example(id="e3", input="q", budget={"dollars": 1}))


async def test_evaluator_scores_every_case_and_results_round_trip(
    trace: Trace, tmp_path: Path
) -> None:
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text(
        '{"id": "good", "input": "q", "expected_tools": ["web_search", "web_fetch"]}\n'
        '{"id": "bad", "input": "q", "expected_tools": ["web_search"], '
        '"budget": {"tool_calls": 1}}\n'
    )
    good, bad = load_examples(dataset)

    evaluator = Evaluator([ToolSelectionAccuracy(), BudgetAdherence()])
    results = await evaluator.evaluate([(trace, good), (trace, bad)])

    assert [(r.example_id, r.metric_id, r.score.passed) for r in results] == [
        ("good", "tool_selection_accuracy", True),
        ("good", "budget_adherence", True),
        ("bad", "tool_selection_accuracy", False),
        ("bad", "budget_adherence", False),
    ]
    assert {r.layer for r in results} == {"component", "outcome"}
    assert summarize(results) == {"budget_adherence": 0.5, "tool_selection_accuracy": 0.75}

    sink_path = tmp_path / "results" / "run.jsonl"
    await JsonlSink(sink_path).write(results)
    assert [json.loads(line)["metric_id"] for line in sink_path.read_text().splitlines()] == [
        r.metric_id for r in results
    ]

    with pytest.raises(ValueError, match="unique"):
        Evaluator([ToolSelectionAccuracy(), ToolSelectionAccuracy()])


async def test_file_source_filters_by_root_attributes(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    root = span("a1", "invoke_agent other", gen_ai__operation__name="invoke_agent", team="blue")
    path.write_text(
        json.dumps(document(root)) + "\n\n" + json.dumps(document(tool("c1", "x"))) + "\n"
    )
    source = OtlpJsonTraceSource(path)

    assert [len(t.spans) async for t in source.load()] == [2]
    blue = TraceFilter(attribute_equals={"team": "blue"})
    assert [t.trace_id async for t in source.load(blue)] == ["0" * 31 + "1"]
    red = TraceFilter(attribute_equals={"team": "red"})
    assert [t async for t in source.load(red)] == []
    assert [t async for t in source.load(TraceFilter(trace_ids=frozenset({"other"})))] == []


def research_trace(answer: str, *pages: str) -> Trace:
    root = span(
        "a1", "invoke_agent x", gen_ai__operation__name="invoke_agent", output__value=answer
    )
    tools = [
        span(
            f"c{i}",
            "execute_tool web_fetch",
            "a1",
            gen_ai__operation__name="execute_tool",
            gen_ai__tool__name="web_fetch",
            output__value=page,
        )
        for i, page in enumerate(pages, start=1)
    ]
    (trace,) = traces_from_otlp([document(root, *tools)])
    return trace


async def test_citation_grounding_passes_when_every_link_was_seen() -> None:
    trace = research_trace(
        "See [A](https://www.a.example/post/) and [B](http://b.example/x).",
        "1. A post\n   https://a.example/post\n",
        "URL: https://b.example/x\n\nbody",
    )
    score = await CitationGrounding().evaluate(trace)
    assert score.passed
    assert score.reason == "2/2 citations grounded"


async def test_citation_grounding_flags_invented_sources() -> None:
    trace = research_trace(
        "See [A](https://a.example/post) and [Made up](https://nowhere.example/x).",
        "https://a.example/post",
    )
    score = await CitationGrounding().evaluate(trace)
    assert not score.passed
    assert score.value == 0.5
    assert "nowhere.example" in score.reason


async def test_citation_grounding_needs_answer_text_and_enough_citations() -> None:
    no_content = research_trace("", "x")
    assert not (await CitationGrounding().evaluate(no_content)).passed
    one_link = research_trace("[A](https://a.example)", "https://a.example")
    assert not (await CitationGrounding(min_citations=3).evaluate(one_link)).passed
