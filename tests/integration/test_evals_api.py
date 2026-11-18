"""Eval results: written by the eval library's Postgres sink, read by the API.

This keeps ``kairos_evals.sinks.SCHEMA_SQL`` and the ``0005_evals`` migration in step:
the sink writes into the Alembic-created tables, and the API serves them back.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_db import create_engine
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


def judged(example: str, metric: str, passed: bool) -> EvalResult:
    return EvalResult(
        metric_id=metric,
        layer="outcome",
        kind="llm_judge",
        trace_id=f"t-{example}",
        example_id=example,
        score=Score(
            value=1.0 if passed else 0.25,
            passed=passed,
            reason="judge says so",
            details={
                "score": 5 if passed else 2,
                "question": f"Q {example}",
                "answer": f"A {example}",
            },
        ),
    )


async def test_the_review_queue_collects_labels_and_reports_calibration(
    engine: AsyncEngine, database_url: str
) -> None:
    # The judge passed e1-e6 and failed e7-e12 on faithfulness.
    results = [judged(f"e{i}", "faithfulness", i <= 6) for i in range(1, 13)]
    run = EvalRun(
        id="evr_judge",
        suite="intent_tools",
        harness="kairos",
        harness_version="0.1.0",
        model="claude-sonnet-5-5",
        started_at=datetime(2026, 11, 18, tzinfo=UTC),
        finished_at=datetime(2026, 11, 18, 0, 5, tzinfo=UTC),
        examples=12,
        summary=summarize_results(results),
    )
    await PostgresSink(engine).write(run, results)

    async with api(database_url, []) as (client, _):
        queue = (await client.get("/evals/review", params={"limit": 50})).json()
        assert len(queue) == 12
        first = queue[0]
        assert first["criterion"].startswith("Is every specific claim in the answer")
        assert first["question"].startswith("Q e")
        assert first["answer"].startswith("A e")

        # The owner agrees with the judge except on two items.
        for item in queue:
            judge_passed = item["judge_passed"]
            disagree = item["example_id"] in ("e1", "e12")
            label = {
                "run_id": item["run_id"],
                "example_id": item["example_id"],
                "metric_id": item["metric_id"],
                "passed": (not judge_passed) if disagree else judge_passed,
            }
            assert (await client.post("/evals/review", json=label)).status_code == 204
        assert (await client.get("/evals/review")).json() == []  # all labeled

        (calibration,) = (await client.get("/evals/calibration")).json()
        assert calibration["metric_id"] == "faithfulness"
        assert (calibration["n"], calibration["agreement"]) == (12, round(10 / 12, 3))
        assert calibration["kappa"] == pytest.approx(0.667, abs=0.001)  # pyright: ignore[reportUnknownMemberType]
        assert calibration["trusted"] is True

        missing = {
            "run_id": "evr_judge",
            "example_id": "nope",
            "metric_id": "faithfulness",
            "passed": True,
        }
        assert (await client.post("/evals/review", json=missing)).status_code == 404


def test_a_failed_gate_notifies_the_owner_once(
    engine: AsyncEngine, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from kairos_worker.cli import app as cli

    base: dict[str, Any] = {
        "harness": "kairos",
        "harness_version": "0.1.0",
        "model": "claude-sonnet-5-5",
        "started_at": "2026-11-18T00:00:00Z",
        "finished_at": "2026-11-18T00:01:00Z",
        "examples": 1,
        "summary": [],
    }
    runs = tmp_path / "runs.jsonl"
    runs.write_text(
        "\n".join(
            [
                json.dumps(base | {"id": "evr_ok", "suite": "scheduling", "passed": True}),
                json.dumps(
                    base
                    | {
                        "id": "evr_bad",
                        "suite": "safety",
                        "passed": False,
                        "gate": {
                            "failures": ["injection_resistance 0.833 is below the floor 1.000"]
                        },
                    }
                ),
            ]
        )
    )
    monkeypatch.setenv("KAIROS_DATABASE_URL", database_url)
    for _ in range(2):
        result = CliRunner().invoke(cli, ["evals-alert", str(runs)])
        assert result.exit_code == 0, result.output
        assert "alerted for safety" in result.output

    rows = asyncio.run(_notifications(database_url))
    assert [r.kind for r in rows] == ["alert"]  # deduplicated per run
    assert rows[0].payload["title"] == "Eval regression in safety"
    assert "below the floor" in rows[0].payload["body"]


async def _notifications(database_url: str) -> list[Any]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return list(await connection.execute(text("SELECT kind, payload FROM notifications")))
    finally:
        await engine.dispose()
