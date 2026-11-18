"""Result sinks: JSONL files, Postgres (for the dashboard) and S3 Parquet (archive).

Each sink writes one ``EvalRun`` with its results. The Postgres and S3 sinks need the
``postgres`` and ``s3`` extras.
"""

import asyncio
import importlib
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairos_evals.types import EvalResult, EvalRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from types_boto3_s3 import S3Client


class JsonlSink:
    """Appends runs to ``runs.jsonl`` and results to ``results.jsonl`` in a directory."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        directory.mkdir(parents=True, exist_ok=True)

    async def write(self, run: EvalRun, results: Sequence[EvalResult]) -> None:
        with (self._dir / "runs.jsonl").open("a") as file:
            file.write(run.model_dump_json() + "\n")
        with (self._dir / "results.jsonl").open("a") as file:
            for result in results:
                file.write(json.dumps({"run_id": run.id, **result.model_dump(mode="json")}) + "\n")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id text PRIMARY KEY,
    suite text NOT NULL,
    harness text NOT NULL,
    harness_version text NOT NULL,
    model text NOT NULL,
    git_sha text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    examples integer NOT NULL,
    passed boolean,
    summary jsonb NOT NULL,
    gate jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS eval_results (
    id bigserial PRIMARY KEY,
    run_id text NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    example_id text,
    trace_id text NOT NULL,
    metric_id text NOT NULL,
    layer text NOT NULL,
    kind text NOT NULL,
    value double precision NOT NULL,
    passed boolean NOT NULL,
    applicable boolean NOT NULL,
    measurement double precision,
    reason text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'
);
"""
"""The tables this sink writes. An application can create them with its own
migrations instead (Kairos does, in ``kairos_db``); ``create_schema`` is for
standalone use."""


class PostgresSink:
    """Writes runs and results in one transaction."""

    def __init__(self, engine: "AsyncEngine") -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as connection:
            for statement in filter(str.strip, SCHEMA_SQL.split(";")):
                await connection.execute(text(statement))

    async def write(self, run: EvalRun, results: Sequence[EvalResult]) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO eval_runs (id, suite, harness, harness_version, model, git_sha,"
                    " started_at, finished_at, examples, passed, summary, gate) VALUES (:id,"
                    " :suite, :harness, :harness_version, :model, :git_sha, :started_at,"
                    " :finished_at, :examples, :passed, CAST(:summary AS jsonb),"
                    " CAST(:gate AS jsonb))"
                ),
                {
                    **run.model_dump(exclude={"summary", "gate"}),
                    "summary": json.dumps([s.model_dump(mode="json") for s in run.summary]),
                    "gate": json.dumps(run.gate),
                },
            )
            if results:
                await connection.execute(
                    text(
                        "INSERT INTO eval_results (run_id, example_id, trace_id, metric_id,"
                        " layer, kind, value, passed, applicable, measurement, reason, details)"
                        " VALUES (:run_id, :example_id, :trace_id, :metric_id, :layer, :kind,"
                        " :value, :passed, :applicable, :measurement, :reason,"
                        " CAST(:details AS jsonb))"
                    ),
                    [_row(run.id, r) for r in results],
                )


def _row(run_id: str, result: EvalResult) -> dict[str, Any]:
    score = result.score
    return {
        "run_id": run_id,
        "example_id": result.example_id,
        "trace_id": result.trace_id,
        "metric_id": result.metric_id,
        "layer": result.layer,
        "kind": result.kind,
        "value": score.value,
        "passed": score.passed,
        "applicable": score.applicable,
        "measurement": score.measurement,
        "reason": score.reason,
        "details": json.dumps(score.details),
    }


class S3ParquetSink:
    """Archives each run to S3, partitioned the way Athena and Glue expect::

        {prefix}/suite={suite}/date={YYYY-MM-DD}/version={harness_version}/{run_id}.parquet
        {prefix}/suite={suite}/date={YYYY-MM-DD}/version={harness_version}/{run_id}.run.json

    ``client`` is a boto3 S3 client; uploads run in a worker thread.
    """

    def __init__(
        self, bucket: str, prefix: str = "evals", *, client: "S3Client | None" = None
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client

    def key(self, run: EvalRun, suffix: str) -> str:
        return (
            f"{self._prefix}/suite={run.suite}/date={run.started_at:%Y-%m-%d}/"
            f"version={run.harness_version}/{run.id}{suffix}"
        )

    async def write(self, run: EvalRun, results: Sequence[EvalResult]) -> None:
        # pyarrow ships no type stubs; it is used through Any on purpose.
        pa: Any = importlib.import_module("pyarrow")
        pq: Any = importlib.import_module("pyarrow.parquet")

        rows = [_row(run.id, r) for r in results]
        table = pa.Table.from_pylist(rows, schema=_parquet_schema(pa))
        buffer = io.BytesIO()
        pq.write_table(table, buffer)
        client = self._client
        if client is None:
            import boto3

            # Only some services' stubs are installed, so boto3's overloads are partial.
            client = boto3.client("s3")  # pyright: ignore[reportUnknownMemberType]
        await asyncio.to_thread(
            client.put_object,
            Bucket=self._bucket,
            Key=self.key(run, ".parquet"),
            Body=buffer.getvalue(),
        )
        await asyncio.to_thread(
            client.put_object,
            Bucket=self._bucket,
            Key=self.key(run, ".run.json"),
            Body=run.model_dump_json().encode(),
            ContentType="application/json",
        )


def _parquet_schema(pa: Any) -> Any:
    return pa.schema(
        [
            ("run_id", pa.string()),
            ("example_id", pa.string()),
            ("trace_id", pa.string()),
            ("metric_id", pa.string()),
            ("layer", pa.string()),
            ("kind", pa.string()),
            ("value", pa.float64()),
            ("passed", pa.bool_()),
            ("applicable", pa.bool_()),
            ("measurement", pa.float64()),
            ("reason", pa.string()),
            ("details", pa.string()),
        ]
    )
