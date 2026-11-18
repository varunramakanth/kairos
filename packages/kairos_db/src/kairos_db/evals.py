"""Read side of eval results for the dashboard. Writes go through
``kairos_evals.PostgresSink`` (the eval CLI), never through the API."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_domain.evals import (
    EvalResultRow,
    EvalRunDetail,
    EvalRunSummary,
    ReviewItem,
    ReviewLabel,
)

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

    async def review_queue(self, *, limit: int = 10) -> list[ReviewItem]:
        """Unlabeled judge verdicts from each suite's latest run, in a stable shuffled
        order (by a hash of example and criterion), so the sample is not cherry-picked."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (suite, model) id, suite
                        FROM eval_runs ORDER BY suite, model, started_at DESC
                    )
                    SELECT r.run_id, latest.suite, r.example_id, r.metric_id, r.passed,
                           r.reason, r.details
                    FROM eval_results r JOIN latest ON latest.id = r.run_id
                    WHERE r.kind = 'llm_judge' AND r.applicable AND r.example_id IS NOT NULL
                      AND r.details ? 'answer'
                      AND NOT EXISTS (
                        SELECT 1 FROM eval_labels l WHERE l.run_id = r.run_id
                          AND l.example_id = r.example_id AND l.metric_id = r.metric_id)
                    ORDER BY md5(r.example_id || r.metric_id)
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
            return [
                ReviewItem(
                    run_id=row.run_id,
                    suite=row.suite,
                    example_id=row.example_id,
                    metric_id=row.metric_id,
                    question=str(row.details.get("question", "")),
                    answer=str(row.details.get("answer", "")),
                    criterion=row.metric_id,
                    judge_passed=row.passed,
                    judge_score=row.details.get("score"),
                    judge_reason=row.reason,
                )
                for row in rows
            ]

    async def label(self, label: ReviewLabel) -> bool:
        """Store (or change) a human label. ``False`` if the verdict does not exist."""
        async with self._engine.begin() as connection:
            exists = (
                await connection.execute(
                    text(
                        "SELECT 1 FROM eval_results WHERE run_id = :run_id "
                        "AND example_id = :example_id AND metric_id = :metric_id"
                    ),
                    label.model_dump(include={"run_id", "example_id", "metric_id"}),
                )
            ).first()
            if exists is None:
                return False
            await connection.execute(
                text(
                    "INSERT INTO eval_labels (run_id, example_id, metric_id, human_passed, note) "
                    "VALUES (:run_id, :example_id, :metric_id, :passed, :note) "
                    "ON CONFLICT (run_id, example_id, metric_id) DO UPDATE SET "
                    "human_passed = EXCLUDED.human_passed, note = EXCLUDED.note, labeled_at = now()"
                ),
                label.model_dump(),
            )
            return True

    async def label_pairs(self) -> dict[str, list[tuple[bool, bool]]]:
        """Per criterion: (judge passed, human passed) for every labeled verdict."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT l.metric_id, r.passed, l.human_passed FROM eval_labels l "
                    "JOIN eval_results r ON r.run_id = l.run_id AND r.example_id = l.example_id "
                    "AND r.metric_id = l.metric_id ORDER BY l.id"
                )
            )
            pairs: dict[str, list[tuple[bool, bool]]] = {}
            for row in rows:
                pairs.setdefault(row.metric_id, []).append((row.passed, row.human_passed))
            return pairs
