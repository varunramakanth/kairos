"use client";

import { Bell, Pause, Play, RotateCw, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useState } from "react";
import { confirmJob, getJob, runJobNow, updateJob } from "@/client/sdk.gen";
import type { JobDetail, JobSpec } from "@/client/types.gen";
import { JobForm } from "@/components/job-form";
import { Sparkline } from "@/components/sparkline";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import {
  describeCondition,
  describeSchedule,
  formatWhen,
  STATUS_LABELS,
} from "@/lib/jobs";

export function JobView({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = use(params);
  const router = useRouter();
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const load = useCallback(async () => {
    const { data, error: failure } = await getJob({ path: { job_id: jobId } });
    if (data) setDetail(data);
    else setError(errorMessage(failure));
  }, [jobId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 10_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function act(
    action: () => Promise<{ error?: unknown; data?: unknown }>,
  ) {
    const { data, error: failure } = await action();
    if (data === undefined) setError(errorMessage(failure));
    else setError(null);
    await load();
  }

  async function save(spec: JobSpec): Promise<string | null> {
    const { data, error: failure } = await updateJob({
      path: { job_id: jobId },
      body: { spec },
    });
    if (!data) return errorMessage(failure);
    setEditing(false);
    await load();
    return null;
  }

  async function remove() {
    if (
      !window.confirm(
        "Delete this job? Its history is kept but it will never run again.",
      )
    )
      return;
    await updateJob({ path: { job_id: jobId }, body: { status: "deleted" } });
    router.push("/tasks");
  }

  if (!detail) {
    return (
      <p
        className={`p-6 ${error ? "text-destructive" : "text-muted-foreground"}`}
      >
        {error ?? "Loading…"}
      </p>
    );
  }

  const { job, runs, observations, alerts } = detail;
  const watch = job.spec.watch;
  const values = [...observations]
    .reverse()
    .map((o) => o.observation.value)
    .filter((v): v is number => typeof v === "number");
  const threshold =
    watch &&
    (watch.condition.kind === "below" || watch.condition.kind === "above")
      ? watch.condition.threshold
      : undefined;
  const path = { path: { job_id: jobId } };

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 overflow-y-auto px-4 md:px-6 py-6">
      <header className="space-y-2">
        <Link
          href="/tasks"
          className="text-muted-foreground text-sm underline-offset-4 hover:underline"
        >
          ← Tasks
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="font-semibold text-xl">{job.spec.name}</h1>
            <p className="text-muted-foreground text-sm">
              {job.spec.kind} · {describeSchedule(job.spec.schedule)} ·{" "}
              {STATUS_LABELS[job.status]}
              {job.consecutive_failures > 0 && (
                <span className="text-destructive">
                  {" "}
                  · {job.consecutive_failures} failed in a row
                </span>
              )}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {job.status === "pending_confirmation" && (
              <Button onClick={() => act(() => confirmJob(path))}>
                Confirm and activate
              </Button>
            )}
            {job.status === "active" && (
              <>
                <Button
                  variant="outline"
                  onClick={() => act(() => runJobNow(path))}
                >
                  <RotateCw />
                  Run now
                </Button>
                <Button
                  variant="outline"
                  onClick={() =>
                    act(() =>
                      updateJob({ ...path, body: { status: "paused" } }),
                    )
                  }
                >
                  <Pause />
                  Pause
                </Button>
              </>
            )}
            {job.status === "paused" && (
              <Button
                onClick={() =>
                  act(() => updateJob({ ...path, body: { status: "active" } }))
                }
              >
                <Play />
                Resume
              </Button>
            )}
            <Button
              variant="outline"
              onClick={() => setEditing((open) => !open)}
            >
              Edit
            </Button>
            <Button variant="destructive" onClick={remove} aria-label="Delete">
              <Trash2 />
            </Button>
          </div>
        </div>
        {error && <p className="text-destructive text-sm">{error}</p>}
      </header>

      {editing && (
        <JobForm
          initial={job.spec}
          submitLabel="Save"
          onSubmit={save}
          onCancel={() => setEditing(false)}
        />
      )}

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Next run" value={formatWhen(job.next_run_at)} />
        <Stat label="Last run" value={formatWhen(job.last_run_at)} />
        <Stat label="Runs" value={String(runs.length)} />
        <Stat label="Alerts" value={String(alerts.length)} />
      </dl>

      {watch && (
        <section className="space-y-2 rounded-lg border p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
            <span>
              <span className="text-muted-foreground">Watching</span>{" "}
              {watch.extract} <span className="text-muted-foreground">at</span>{" "}
              {watch.target}
            </span>
            <span className="text-muted-foreground">
              alert when {describeCondition(watch.condition)}
            </span>
          </div>
          <Sparkline
            values={values}
            threshold={threshold}
            width={600}
            height={80}
          />
        </section>
      )}

      {alerts.length > 0 && (
        <section>
          <h2 className="mb-2 font-medium">Alerts</h2>
          <ul className="space-y-1">
            {alerts.map((alert) => (
              <li key={alert.id} className="flex items-start gap-2 text-sm">
                <Bell className="mt-0.5 size-4 shrink-0 text-amber-600" />
                <span>
                  <span className="text-muted-foreground">
                    {formatWhen(alert.created_at)} ·{" "}
                  </span>
                  {alert.message}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h2 className="mb-2 font-medium">History</h2>
        {runs.length === 0 ? (
          <p className="text-muted-foreground text-sm">No runs yet.</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full min-w-[36rem] text-sm">
              <thead className="bg-muted/50 text-left text-muted-foreground text-xs">
                <tr>
                  <th className="px-3 py-2 font-normal">Scheduled for</th>
                  <th className="px-3 py-2 font-normal">Result</th>
                  <th className="px-3 py-2 font-normal">Observation</th>
                  <th className="px-3 py-2 font-normal">Trace</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {runs.map((run) => {
                  const seen = observations.find(
                    (o) => o.run_id === run.run_id,
                  );
                  return (
                    <tr key={run.id}>
                      <td className="px-3 py-1.5 text-xs">
                        {formatWhen(run.scheduled_for)}
                      </td>
                      <td
                        className={`px-3 py-1.5 text-xs ${run.status === "failed" ? "text-destructive" : ""}`}
                      >
                        {run.status}
                        {run.error ? `: ${run.error}` : ""}
                      </td>
                      <td className="px-3 py-1.5 text-xs">
                        {seen
                          ? seen.observation.found
                            ? `${seen.observation.value ?? seen.observation.text}${seen.observation.unit ? ` ${seen.observation.unit}` : ""}${seen.matched ? " ✓" : ""}`
                            : "not found"
                          : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-xs">
                        <Link
                          href={`/runs/${run.run_id}`}
                          className="underline-offset-4 hover:underline"
                        >
                          view
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border px-3 py-2">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="truncate font-medium text-sm">{value}</dd>
    </div>
  );
}
