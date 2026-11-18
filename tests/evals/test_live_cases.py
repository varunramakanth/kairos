"""Live-recorded golden cases, replayed offline (no network, no cost).

The model turns and tool results are real recordings (``just record-live``), so the
scores describe actual behaviour on the recording date. Gated: every citation is
grounded in what the agent read, and the run stays within its budget. Reported but
not gated: which tools it chose, since a live run legitimately varies there.
"""

from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from kairos_domain.events import RunFinished
from kairos_domain.run import RunRequest, StopReason
from kairos_evals import (
    Evaluator,
    Example,
    OtlpJsonTraceSource,
    TraceFilter,
    load_examples,
)
from kairos_evals.metrics import BudgetAdherence, CitationGrounding, ToolSelectionAccuracy
from kairos_harness import AgentLoop, InMemoryCheckpointer
from kairos_harness.cassette import ReplayProvider, ReplayToolExecutor
from kairos_telemetry import OtlpJsonFileExporter, TracingHooks, configure_tracing
from tests.evals.live_cases import DATASET, MODEL, model_cassette, tool_cassette

pytestmark = pytest.mark.evals

GATED = {"citation_grounding", "budget_adherence"}


@pytest.mark.parametrize("example", load_examples(DATASET), ids=lambda e: e.id)
async def test_live_case_replays_and_meets_the_gate(example: Example, tmp_path: Path) -> None:
    traces = tmp_path / "traces.jsonl"
    tracing = configure_tracing(
        service_version="eval", processors=[SimpleSpanProcessor(OtlpJsonFileExporter(traces))]
    )
    provider = ReplayProvider(model_cassette(example.id), name="anthropic")
    loop = AgentLoop(
        provider=provider,
        tools=ReplayToolExecutor(tool_cassette(example.id)),
        checkpointer=InMemoryCheckpointer(),
        hooks=TracingHooks(
            tracing.get_tracer("kairos"),
            provider_name=provider.name,
            harness_version="eval",
            capture_content=True,
        ),
    )
    events = [
        e async for e in loop.run(RunRequest(run_id=example.id, input=example.input, model=MODEL))
    ]
    (finished,) = (e for e in events if isinstance(e, RunFinished))
    assert finished.stop_reason is StopReason.COMPLETED, finished.error

    (trace,) = [
        t
        async for t in OtlpJsonTraceSource(traces).load(
            TraceFilter(attribute_equals={"kairos.run_id": example.id})
        )
    ]
    metrics = [CitationGrounding(min_citations=3), BudgetAdherence(), ToolSelectionAccuracy()]
    results = await Evaluator(metrics).evaluate([(trace, example)])

    report = {r.metric_id: r.score for r in results}
    print({k: (round(v.value, 2), v.reason) for k, v in report.items()})
    failures = [f"{k}: {v.reason}" for k, v in report.items() if k in GATED and not v.passed]
    assert not failures, failures
