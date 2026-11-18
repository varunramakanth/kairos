"""Read side of eval results for the dashboard. Writes go through
``kairos_evals.PostgresSink`` (the eval CLI), never through the API."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_domain.evals import EvalResultRow, EvalRunDetail, EvalRunSummary

_RUN_COLUMNS = (
    "id, suite, harness, harness_version, model, git_sha, started_at, finished_at, "
    "examples, passed, summary, gate"
)


class EvalStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def runs(self, *, suite: str | None = None, limit: int = 100) -> list[EvalRunSummary]:
        """Newest first."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    f"SELECT {_RUN_COLUMNS} FROM eval_runs "  # noqa: S608  # constant columns
                    "WHERE (CAST(:suite AS text) IS NULL OR suite = :suite) "
                    "ORDER BY started_at DESC LIMIT :limit"
                ),
                {"suite": suite, "limit": limit},
            )
            return [EvalRunSummary.model_validate(dict(r)) for r in rows.mappings()]

    async def detail(self, run_id: str) -> EvalRunDetail | None:
        async with self._engine.connect() as connection:
            run = (
                (
                    await connection.execute(
                        text(f"SELECT {_RUN_COLUMNS} FROM eval_runs WHERE id = :id"),  # noqa: S608
                        {"id": run_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                return None
            rows = await connection.execute(
                text(
                    "SELECT example_id, trace_id, metric_id, layer, kind, value, passed, "
                    "applicable, measurement, reason, details FROM eval_results "
                    "WHERE run_id = :id ORDER BY id"
                ),
                {"id": run_id},
            )
            return EvalRunDetail(
                run=EvalRunSummary.model_validate(dict(run)),
                results=[EvalResultRow.model_validate(dict(r)) for r in rows.mappings()],
            )
