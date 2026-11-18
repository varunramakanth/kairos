"""Eval results: written by the eval library's Postgres sink, read by the API.

This keeps ``kairos_evals.sinks.SCHEMA_SQL`` and the ``0005_evals`` migration in step:
the sink writes into the Alembic-created tables, and the API serves them back.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_evals import EvalResult, EvalRun, PostgresSink, Score, summarize_results
from tests.integration.test_governance import api

pytestmark = pytest.mark.integration


def result(
    example: str, metric: str, value: float, *, measurement: float | None = None
) -> EvalResult:
    return EvalResult(
        metric_id=metric,
        layer="component" if metric != "cost_per_task" else "outcome",
        kind="deterministic",
        trace_id=f"trace-{example}",
        example_id=example,
        score=Score(
            value=value,
            passed=value == 1.0,
            reason="ok" if value == 1.0 else "expected research, got chat",
            measurement=measurement,
            details={"expected": "research"},
        ),
    )


async def test_sink_writes_and_the_api_reads_eval_runs(
    engine: AsyncEngine, database_url: str
) -> None:
    results = [
        result("news", "intent_accuracy", 1.0),
        result("weather", "intent_accuracy", 0.0),
        result("news", "cost_per_task", 1.0, measurement=0.012),
    ]
    older, newer = (
        EvalRun(
            id=run_id,
            suite="intent_tools",
            harness="kairos",
            harness_version="0.1.0",
            model="claude-sonnet-5-5",
            started_at=datetime(2026, 11, day, tzinfo=UTC),
            finished_at=datetime(2026, 11, day, 0, 5, tzinfo=UTC),
            git_sha="abc1234",
            examples=2,
            summary=summarize_results(results),
            passed=True,
            gate={"failures": []},
        )
        for run_id, day in (("evr_old", 17), ("evr_new", 18))
    )
    sink = PostgresSink(engine)
    await sink.write(older, results)
    await sink.write(newer, results)

    async with api(database_url, []) as (client, _):
        runs = (await client.get("/evals/runs", params={"suite": "intent_tools"})).json()
        assert [r["id"] for r in runs] == ["evr_new", "evr_old"]
        summary = {s["metric_id"]: s for s in runs[0]["summary"]}
        assert summary["intent_accuracy"]["mean"] == 0.5
        assert summary["cost_per_task"]["p50"] == 0.012
        assert (await client.get("/evals/runs", params={"suite": "other"})).json() == []

        detail = (await client.get("/evals/runs/evr_new")).json()
        assert detail["run"]["passed"] is True
        assert [r["metric_id"] for r in detail["results"]] == [
            "intent_accuracy",
            "intent_accuracy",
            "cost_per_task",
        ]
        assert detail["results"][1]["reason"] == "expected research, got chat"
        assert detail["results"][1]["details"] == {"expected": "research"}
        assert (await client.get("/evals/runs/nope")).status_code == 404
