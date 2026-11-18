"""M1 eval suite: golden cases replayed from cassettes, scored from their traces.

The whole path is exercised: cassette -> ``AgentLoop`` -> tools -> OpenTelemetry
spans -> OTLP/JSON file -> ``kairos_evals`` -> scores. The cassettes are synthetic
(see ``scenarios.py``), so this gates determinism and wiring, not model quality.
"""

from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from kairos_domain.content import ToolResultBlock
from kairos_domain.events import RunFinished
from kairos_domain.run import RunRequest, StopReason
from kairos_evals import (
    Evaluator,
    Example,
    OtlpJsonTraceSource,
    Trace,
    TraceFilter,
    load_examples,
    summarize,
)
from kairos_evals.metrics import BudgetAdherence, ToolSelectionAccuracy
from kairos_harness import AgentLoop, InMemoryCheckpointer, SeededIdGenerator
from kairos_harness.cassette import ReplayProvider
from kairos_harness.context import UNTRUSTED_TAG
from kairos_telemetry import OtlpJsonFileExporter, TracingHooks, configure_tracing
from tests.evals.scenarios import CASSETTES, DATASET, MODEL, build_tools

pytestmark = pytest.mark.evals

EXAMPLES = load_examples(DATASET)


async def run_example(example: Example, traces_path: Path) -> InMemoryCheckpointer:
    """Replay one golden case through the harness, exporting its trace to a file."""
    tracing = configure_tracing(
        service_version="test",
        processors=[SimpleSpanProcessor(OtlpJsonFileExporter(traces_path))],
    )
    provider = ReplayProvider(CASSETTES / f"{example.id}.jsonl", name="anthropic")
    checkpointer = InMemoryCheckpointer()
    loop = AgentLoop(
        provider=provider,
        tools=build_tools(),
        checkpointer=checkpointer,
        ids=SeededIdGenerator(example.id),
        hooks=TracingHooks(
            tracing.get_tracer("kairos"), provider_name=provider.name, harness_version="test"
        ),
    )
    request = RunRequest(run_id=example.id, input=example.input, model=MODEL)
    events = [event async for event in loop.run(request)]
    (finished,) = (e for e in events if isinstance(e, RunFinished))
    assert finished.stop_reason is StopReason.COMPLETED, finished.error
    return checkpointer


async def trace_for(example: Example, traces_path: Path) -> Trace:
    source = OtlpJsonTraceSource(traces_path)
    filter_ = TraceFilter(attribute_equals={"kairos.run_id": example.id})
    (trace,) = [t async for t in source.load(filter_)]
    return trace


async def test_golden_cases_meet_the_regression_gate(tmp_path: Path) -> None:
    traces_path = tmp_path / "traces.jsonl"
    cases: list[tuple[Trace, Example | None]] = []
    for example in EXAMPLES:
        await run_example(example, traces_path)
        cases.append((await trace_for(example, traces_path), example))

    results = await Evaluator([ToolSelectionAccuracy(), BudgetAdherence()]).evaluate(cases)

    failures = [
        f"{r.example_id} {r.metric_id}: {r.score.reason}" for r in results if not r.score.passed
    ]
    assert not failures
    assert summarize(results) == {"budget_adherence": 1.0, "tool_selection_accuracy": 1.0}
    assert len(results) == 2 * len(EXAMPLES) == 10


async def test_replay_is_deterministic(tmp_path: Path) -> None:
    example = next(e for e in EXAMPLES if e.id == "parallel_tools")
    first = await run_example(example, tmp_path / "a.jsonl")
    second = await run_example(example, tmp_path / "b.jsonl")

    def comparable(checkpointer: InMemoryCheckpointer) -> list[dict[str, object]]:
        # Timestamps and measured durations are the only things allowed to differ.
        return [
            e.model_dump(exclude={"ts", "duration_ms", "spent"})
            for e in checkpointer.events(example.id)
        ]

    assert comparable(first) == comparable(second)


async def test_injected_page_reaches_the_model_only_as_wrapped_data(tmp_path: Path) -> None:
    example = next(e for e in EXAMPLES if e.id == "injected_page")
    checkpointer = await run_example(example, tmp_path / "traces.jsonl")

    state = await checkpointer.load(example.id)
    assert state is not None
    (result,) = (
        block
        for message in state.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert "ignore previous instructions" in result.content
    assert result.content.startswith(f'<{UNTRUSTED_TAG} source="web_search">')
    assert result.content.endswith(f"</{UNTRUSTED_TAG}>")
    # No tool other than the search ran: the planted instruction was not acted on.
    trace = await trace_for(example, tmp_path / "traces.jsonl")
    assert trace.tool_names == ["web_search"]
