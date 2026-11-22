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

from kairos_db.governance import insert_audit, insert_notification
from kairos_db.queue import insert_run
from kairos_domain.content import Message
from kairos_domain.governance import DigestData, DigestJob, Notification, NotificationKind
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
    "id, user_id, spec, status, next_run_at, last_run_at, consecutive_failures, "
    "created_at, updated_at"
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
        owner: str,
        status: JobStatus,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO jobs (id, user_id, spec, status, next_run_at, created_at, "
                    "updated_at) VALUES (:id, :owner, CAST(:spec AS jsonb), :status, :next, "
                    ":now, :now) ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": job_id,
                    "owner": owner,
                    "spec": spec.model_dump_json(),
                    "status": status.value,
                    "next": next_run_at,
                    "now": now,
                },
            )
            job = await _get(connection, job_id, owner=owner)
        if job is None:
            raise ValueError(f"job {job_id} belongs to another user")
        return job

    async def get(self, job_id: str, *, owner: str) -> Job | None:
        async with self._engine.connect() as connection:
            return await _get(connection, job_id, owner=owner)

    async def list_jobs(self, *, owner: str, include_deleted: bool = False) -> Sequence[Job]:
        where = "" if include_deleted else "AND status <> 'deleted'"
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE user_id = :owner {where} "  # noqa: S608
                    "ORDER BY created_at DESC"
                ),
                {"owner": owner},
            )
            return [_job(row) for row in rows.mappings()]

    async def set_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        owner: str,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job | None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET status = :status, next_run_at = :next, "
                    "consecutive_failures = CASE WHEN :status = 'active' THEN 0 "
                    "ELSE consecutive_failures END, updated_at = :now "
                    "WHERE id = :id AND user_id = :owner"
                ),
                {
                    "id": job_id,
                    "owner": owner,
                    "status": status.value,
                    "next": next_run_at,
                    "now": now,
                },
            )
            return await _get(connection, job_id, owner=owner)

    async def update_spec(
        self,
        job_id: str,
        spec: JobSpec,
        *,
        owner: str,
        next_run_at: datetime | None,
        now: datetime,
    ) -> Job | None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET spec = CAST(:spec AS jsonb), "
                    "next_run_at = CASE WHEN status = 'active' THEN :next ELSE next_run_at END, "
                    "updated_at = :now WHERE id = :id AND user_id = :owner"
                ),
                {
                    "id": job_id,
                    "owner": owner,
                    "spec": spec.model_dump_json(),
                    "next": next_run_at,
                    "now": now,
                },
            )
            return await _get(connection, job_id, owner=owner)

    async def run_now(self, job_id: str, *, owner: str, now: datetime) -> Job | None:
        """Make an active job due immediately."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET next_run_at = :now, updated_at = :now "
                    "WHERE id = :id AND user_id = :owner AND status = 'active'"
                ),
                {"id": job_id, "owner": owner, "now": now},
            )
            return await _get(connection, job_id, owner=owner)

    async def pause_all(self, *, owner: str, now: datetime) -> int:
        """Pause every active job of ``owner``."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET status = 'paused', updated_at = :now "
                    "WHERE status = 'active' AND user_id = :owner"
                ),
                {"now": now, "owner": owner},
            )
        return result.rowcount

    async def summaries(self, *, owner: str) -> list[JobSummary]:
        jobs = await self.list_jobs(owner=owner)
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

    async def detail(self, job_id: str, *, owner: str, limit: int = 100) -> JobDetail | None:
        async with self._engine.connect() as connection:
            job = await _get(connection, job_id, owner=owner)
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

    async def fire_digest(
        self,
        claim: Claim,
        *,
        job_run_id: str,
        scheduled_for: datetime,
        next_run_at: datetime | None,
        notification: Notification,
        now: datetime,
    ) -> bool:
        """Record a digest firing and queue its notification. No agent run."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET next_run_at = :next, last_run_at = :now, lease_id = NULL, "
                    "lease_until = NULL, updated_at = :now WHERE id = :id AND lease_id = :lease"
                ),
                {"next": next_run_at, "now": now, "id": claim.job.id, "lease": claim.lease_id},
            )
            if result.rowcount != 1:
                return False
            await connection.execute(
                text(
                    "INSERT INTO job_runs (id, job_id, run_id, scheduled_for, status, created_at, "
                    "finished_at) VALUES (:id, :job, NULL, :slot, 'succeeded', :now, :now)"
                ),
                {"id": job_run_id, "job": claim.job.id, "slot": scheduled_for, "now": now},
            )
            await insert_notification(
                connection,
                notification,
                user_id=claim.job.user_id,
                dedupe_key=f"digest:{claim.job.id}:{scheduled_for.isoformat()}",
            )
            await insert_audit(
                connection,
                user_id=claim.job.user_id,
                actor="system",
                action="digest.queued",
                subject=claim.job.id,
            )
        return True

    async def digest_data(self, *, owner: str, since: datetime, until: datetime) -> DigestData:
        params = {"owner": owner, "since": since, "until": until}
        async with self._engine.connect() as connection:
            alerts = await connection.execute(
                text(
                    "SELECT j.spec->>'name' AS name, a.message FROM alerts a JOIN jobs j "
                    "ON j.id = a.job_id WHERE j.user_id = :owner "
                    "AND a.created_at >= :since AND a.created_at < :until "
                    "ORDER BY a.created_at"
                ),
                params,
            )
            jobs = await connection.execute(
                text(
                    """
                    SELECT j.id, j.spec->>'name' AS name, j.spec->>'kind' AS kind, j.status,
                           count(r.id) AS runs,
                           count(r.id) FILTER (WHERE r.status = 'failed') AS failures,
                           (SELECT o.observation FROM watch_observations o
                             WHERE o.job_id = j.id ORDER BY o.created_at DESC LIMIT 1) AS latest
                      FROM jobs j
                      JOIN job_runs r ON r.job_id = j.id
                       AND r.created_at >= :since AND r.created_at < :until
                     WHERE j.spec->>'kind' <> 'digest' AND j.user_id = :owner
                     GROUP BY j.id ORDER BY j.spec->>'name'
                    """
                ),
                params,
            )
            pending = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM approvals a JOIN runs r ON r.id = a.run_id "
                        "WHERE a.status = 'pending' AND r.user_id = :owner"
                    ),
                    params,
                )
            ).scalar_one()
            spend = (
                await connection.execute(
                    text(
                        "SELECT count(*) AS runs, COALESCE(sum((state->>'cost_usd')::float), 0) "
                        "AS cost FROM runs WHERE user_id = :owner "
                        "AND created_at >= :since AND created_at < :until"
                    ),
                    params,
                )
            ).one()
            job_rows = [
                DigestJob(
                    name=row.name,
                    kind=row.kind,
                    runs=int(row.runs),
                    failures=int(row.failures),
                    status=row.status,
                    latest=_latest_text(row.latest),
                )
                for row in jobs
            ]
            return DigestData(
                since=since,
                until=until,
                alerts=[f"{row.name}: {row.message}" for row in alerts],
                jobs=job_rows,
                pending_approvals=int(pending),
                runs=int(spend.runs),
                cost_usd=float(spend.cost),
            )

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
                         WHERE jr.status = 'started' AND r.status = 'finished'
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
            job = await _get(connection, row["job_id"], owner=None, lock=True)
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


async def _get(
    connection: AsyncConnection, job_id: str, *, owner: str | None, lock: bool = False
) -> Job | None:
    """A job by id; with ``owner``, only if it is theirs. ``None`` is the scheduler."""
    suffix = " FOR UPDATE" if lock else ""
    row = (
        (
            await connection.execute(
                text(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = :id "  # noqa: S608
                    f"AND (CAST(:owner AS text) IS NULL OR user_id = :owner){suffix}"
                ),
                {"id": job_id, "owner": owner},
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
        await insert_notification(
            connection,
            Notification(
                kind=NotificationKind.ALERT,
                title=f"{job.spec.name}: alert",
                body=outcome.alert,
                url=f"/tasks/{job.id}",
            ),
            user_id=job.user_id,
            dedupe_key=f"alert:{job.id}:{outcome.dedupe_key}:{run_id}",
        )
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


def _latest_text(raw: Any) -> str | None:
    if not raw:
        return None
    observation = Observation.model_validate(raw)
    if not observation.found:
        return "not found"
    if observation.value is not None:
        return f"{observation.value:g}" + (f" {observation.unit}" if observation.unit else "")
    return observation.text[:80] or None
