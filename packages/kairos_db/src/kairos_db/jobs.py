"""Jobs on Postgres: the ``SchedulerStore`` (see ``kairos_scheduler.protocols``).

The queue mechanics mirror ``RunQueue`` (ADR-003): claim with ``SKIP LOCKED`` and a
fresh ``lease_id``, then condition every later write on it. Two differences:

- Time comes from the caller (``now``), not ``now()`` in SQL, so the scheduler's
  injected clock decides what is due. Tests move that clock across DST changes.
- Firing is one transaction: the job run, the agent run and the job's next
  ``next_run_at``. A unique ``(job_id, scheduled_for)`` constraint backs up the
  lease, so a slot can never produce two runs.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kairos_db.queue import insert_run
from kairos_domain.content import Message
from kairos_domain.jobs import (
    Alert,
    Job,
    JobDetail,
    JobRun,
    JobSpec,
    JobStatus,
    JobSummary,
    Observation,
    StoredObservation,
)
from kairos_domain.run import StopReason
from kairos_domain.scheduling import Claim, Finished, Firing, PreviousAlert, Settlement

_MESSAGES = TypeAdapter[list[Message]](list[Message])
_JOB_COLUMNS = (
    "id, spec, status, next_run_at, last_run_at, consecutive_failures, created_at, updated_at"
)


def _job(row: RowMapping) -> Job:
    data: dict[str, Any] = dict(row)
    return Job.model_validate({**data, "spec": JobSpec.model_validate(data["spec"])})


class JobStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # --- JobRepository -----------------------------------------------------------

    async def create(
        self,
        job_id: str,
        spec: JobSpec,
        *,
        status: JobStatus,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO jobs (id, spec, status, next_run_at, created_at, updated_at) "
                    "VALUES (:id, CAST(:spec AS jsonb), :status, :next, :now, :now) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": job_id,
                    "spec": spec.model_dump_json(),
                    "status": status.value,
                    "next": next_run_at,
                    "now": now,
                },
            )
            job = await _get(connection, job_id)
        assert job is not None  # noqa: S101 (just inserted or already present)
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._engine.connect() as connection:
            return await _get(connection, job_id)

    async def list_jobs(self, *, include_deleted: bool = False) -> Sequence[Job]:
        where = "" if include_deleted else "WHERE status <> 'deleted'"
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(f"SELECT {_JOB_COLUMNS} FROM jobs {where} ORDER BY created_at DESC")  # noqa: S608
            )
            return [_job(row) for row in rows.mappings()]

    async def set_status(
        self, job_id: str, status: JobStatus, *, next_run_at: datetime | None, now: datetime
    ) -> Job | None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET status = :status, next_run_at = :next, "
                    "consecutive_failures = CASE WHEN :status = 'active' THEN 0 "
                    "ELSE consecutive_failures END, updated_at = :now WHERE id = :id"
                ),
                {"id": job_id, "status": status.value, "next": next_run_at, "now": now},
            )
            return await _get(connection, job_id)

    async def update_spec(
        self, job_id: str, spec: JobSpec, *, next_run_at: datetime | None, now: datetime
    ) -> Job | None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET spec = CAST(:spec AS jsonb), "
                    "next_run_at = CASE WHEN status = 'active' THEN :next ELSE next_run_at END, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"id": job_id, "spec": spec.model_dump_json(), "next": next_run_at, "now": now},
            )
            return await _get(connection, job_id)

    async def run_now(self, job_id: str, *, now: datetime) -> Job | None:
        """Make an active job due immediately."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET next_run_at = :now, updated_at = :now "
                    "WHERE id = :id AND status = 'active'"
                ),
                {"id": job_id, "now": now},
            )
            return await _get(connection, job_id)

    async def pause_all(self, *, now: datetime) -> int:
        """The kill switch: pause every active job."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET status = 'paused', updated_at = :now WHERE status = 'active'"
                ),
                {"now": now},
            )
        return result.rowcount

    async def summaries(self) -> list[JobSummary]:
        jobs = await self.list_jobs()
        if not jobs:
            return []
        ids = [job.id for job in jobs]
        async with self._engine.connect() as connection:
            last_runs = await connection.execute(
                text(
                    "SELECT DISTINCT ON (job_id) id, job_id, run_id, scheduled_for, status, "
                    "error, created_at, finished_at FROM job_runs WHERE job_id = ANY(:ids) "
                    "ORDER BY job_id, created_at DESC"
                ),
                {"ids": ids},
            )
            runs = {r["job_id"]: JobRun.model_validate(dict(r)) for r in last_runs.mappings()}
            values = await connection.execute(
                text(
                    "SELECT job_id, (observation->>'value')::float AS value FROM ("
                    "  SELECT job_id, observation, created_at, row_number() OVER ("
                    "    PARTITION BY job_id ORDER BY created_at DESC) AS n"
                    "  FROM watch_observations WHERE job_id = ANY(:ids)"
                    "    AND observation->>'value' IS NOT NULL) recent "
                    "WHERE n <= 30 ORDER BY job_id, created_at"
                ),
                {"ids": ids},
            )
            series: dict[str, list[float]] = {}
            for row in values:
                series.setdefault(row.job_id, []).append(float(row.value))
            counts = await connection.execute(
                text(
                    "SELECT job_id, count(*) AS n FROM alerts WHERE job_id = ANY(:ids) "
                    "GROUP BY job_id"
                ),
                {"ids": ids},
            )
            alerts = {row.job_id: int(row.n) for row in counts}
        return [
            JobSummary(
                job=job,
                last_run=runs.get(job.id),
                recent_values=series.get(job.id, []),
                alert_count=alerts.get(job.id, 0),
            )
            for job in jobs
        ]

    async def detail(self, job_id: str, *, limit: int = 100) -> JobDetail | None:
        async with self._engine.connect() as connection:
            job = await _get(connection, job_id)
            if job is None:
                return None
            runs = await connection.execute(
                text(
                    "SELECT id, job_id, run_id, scheduled_for, status, error, created_at, "
                    "finished_at FROM job_runs WHERE job_id = :id "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"id": job_id, "limit": limit},
            )
            observations = await connection.execute(
                text(
                    "SELECT id, job_id, run_id, observation, matched, created_at "
                    "FROM watch_observations WHERE job_id = :id "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"id": job_id, "limit": limit},
            )
            alerts = await connection.execute(
                text(
                    "SELECT id, job_id, run_id, message, dedupe_key, created_at FROM alerts "
                    "WHERE job_id = :id ORDER BY created_at DESC LIMIT :limit"
                ),
                {"id": job_id, "limit": limit},
            )
            return JobDetail(
                job=job,
                runs=[JobRun.model_validate(dict(r)) for r in runs.mappings()],
                observations=[
                    StoredObservation.model_validate(dict(r)) for r in observations.mappings()
                ],
                alerts=[Alert.model_validate(dict(r)) for r in alerts.mappings()],
            )

    # --- SchedulerStore ----------------------------------------------------------

    async def claim_due(self, *, now: datetime, ttl_seconds: float) -> Claim | None:
        lease_id = uuid.uuid4().hex
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            f"""
                        UPDATE jobs SET lease_id = :lease, lease_until = :until
                         WHERE id = (
                               SELECT j.id FROM jobs j
                                WHERE j.status = 'active'
                                  AND j.next_run_at <= :now
                                  AND (j.lease_until IS NULL OR j.lease_until < :now)
                                  AND NOT EXISTS (
                                      SELECT 1 FROM job_runs r
                                       WHERE r.job_id = j.id AND r.status = 'started')
                                ORDER BY j.next_run_at
                                  FOR UPDATE SKIP LOCKED
                                LIMIT 1)
                        RETURNING {_JOB_COLUMNS}
                        """  # noqa: S608 (constant column list)
                        ),
                        {
                            "lease": lease_id,
                            "until": now + timedelta(seconds=ttl_seconds),
                            "now": now,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return Claim(job=_job(row), lease_id=lease_id) if row else None

    async def fire(self, claim: Claim, firing: Firing, *, now: datetime) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET next_run_at = :next, status = :status, last_run_at = :now, "
                    "lease_id = NULL, lease_until = NULL, updated_at = :now "
                    "WHERE id = :id AND lease_id = :lease"
                ),
                {
                    "next": firing.next_run_at,
                    "status": firing.status.value,
                    "now": now,
                    "id": claim.job.id,
                    "lease": claim.lease_id,
                },
            )
            if result.rowcount != 1:
                return False
            await insert_run(connection, firing.request)
            await connection.execute(
                text(
                    "INSERT INTO job_runs (id, job_id, run_id, scheduled_for, status, created_at) "
                    "VALUES (:id, :job, :run, :slot, 'started', :now)"
                ),
                {
                    "id": firing.job_run_id,
                    "job": claim.job.id,
                    "run": firing.request.run_id,
                    "slot": firing.scheduled_for,
                    "now": now,
                },
            )
        return True

    async def close(self, claim: Claim, status: JobStatus, *, now: datetime) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET status = :status, next_run_at = NULL, lease_id = NULL, "
                    "lease_until = NULL, updated_at = :now WHERE id = :id AND lease_id = :lease"
                ),
                {"status": status.value, "now": now, "id": claim.job.id, "lease": claim.lease_id},
            )
        return result.rowcount == 1

    async def settle_next(self, decide: Callable[[Finished], Settlement], *, now: datetime) -> bool:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT jr.id, jr.job_id, jr.run_id, jr.scheduled_for, jr.status,
                               jr.error, jr.created_at, jr.finished_at,
                               r.stop_reason AS run_stop_reason,
                               r.state->>'error' AS run_error,
                               r.state->'messages' AS run_messages
                          FROM job_runs jr JOIN runs r ON r.id = jr.run_id
                         WHERE jr.status = 'started' AND r.status IN ('finished', 'paused')
                         ORDER BY jr.created_at
                           FOR UPDATE OF jr SKIP LOCKED
                         LIMIT 1
                        """
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            job = await _get(connection, row["job_id"], lock=True)
            assert job is not None  # noqa: S101 (foreign key)
            job_run = JobRun.model_validate({k: row[k] for k in JobRun.model_fields if k in row})
            finished = Finished(
                job=job,
                job_run=job_run,
                stop_reason=StopReason(row["run_stop_reason"]) if row["run_stop_reason"] else None,
                error=row["run_error"],
                messages=_MESSAGES.validate_python(row["run_messages"] or []),
                previous=await _last_observation(connection, job.id),
                last_alert=await _last_alert(connection, job.id),
            )
            outcome = decide(finished)
            await _record(connection, finished, outcome, now=now)
        return True


async def _get(connection: AsyncConnection, job_id: str, *, lock: bool = False) -> Job | None:
    suffix = " FOR UPDATE" if lock else ""
    row = (
        (
            await connection.execute(
                text(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = :id{suffix}"),  # noqa: S608
                {"id": job_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return _job(row) if row else None


async def _last_observation(connection: AsyncConnection, job_id: str) -> Observation | None:
    row = (
        await connection.execute(
            text(
                "SELECT observation FROM watch_observations WHERE job_id = :id "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"id": job_id},
        )
    ).one_or_none()
    return Observation.model_validate(row.observation) if row else None


async def _last_alert(connection: AsyncConnection, job_id: str) -> PreviousAlert | None:
    row = (
        await connection.execute(
            text(
                "SELECT dedupe_key, created_at FROM alerts WHERE job_id = :id "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"id": job_id},
        )
    ).one_or_none()
    return PreviousAlert(dedupe_key=row.dedupe_key, at=row.created_at) if row else None


async def _record(
    connection: AsyncConnection, finished: Finished, outcome: Settlement, *, now: datetime
) -> None:
    job, run_id = finished.job, finished.job_run.run_id
    await connection.execute(
        text(
            "UPDATE job_runs SET status = :status, error = :error, finished_at = :now "
            "WHERE id = :id"
        ),
        {
            "status": outcome.status.value,
            "error": outcome.error,
            "now": now,
            "id": finished.job_run.id,
        },
    )
    await connection.execute(
        text(
            "UPDATE jobs SET consecutive_failures = :failures, "
            "next_run_at = COALESCE(:next, next_run_at), updated_at = :now WHERE id = :id"
        ),
        {
            "failures": outcome.consecutive_failures,
            "next": outcome.next_run_at if job.status is JobStatus.ACTIVE else None,
            "now": now,
            "id": job.id,
        },
    )
    if outcome.observation is not None:
        await connection.execute(
            text(
                "INSERT INTO watch_observations (job_id, run_id, observation, matched, created_at) "
                "VALUES (:job, :run, CAST(:obs AS jsonb), :matched, :now)"
            ),
            {
                "job": job.id,
                "run": run_id,
                "obs": outcome.observation.model_dump_json(),
                "matched": outcome.matched,
                "now": now,
            },
        )
    if outcome.alert is not None and outcome.dedupe_key is not None:
        await connection.execute(
            text(
                "INSERT INTO alerts (job_id, run_id, message, dedupe_key, created_at) "
                "VALUES (:job, :run, :message, :key, :now)"
            ),
            {
                "job": job.id,
                "run": run_id,
                "message": outcome.alert,
                "key": outcome.dedupe_key,
                "now": now,
            },
        )
