"use client";

import { Bell, Pause, Plus } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { createJob, listJobs, pauseAllJobs } from "@/client/sdk.gen";
import type { JobSpec, JobSummary } from "@/client/types.gen";
import { JobForm } from "@/components/job-form";
import { Sparkline } from "@/components/sparkline";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { describeSchedule, formatWhen, STATUS_LABELS } from "@/lib/jobs";

export default function TasksPage() {
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    const { data, error: failure } = await listJobs();
    if (data) setJobs(data);
    else setError(errorMessage(failure));
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function create(spec: JobSpec): Promise<string | null> {
    const { data, error: failure } = await createJob({ body: spec });
    if (!data) return errorMessage(failure);
    setCreating(false);
    await load();
    return null;
  }

  async function pauseAll() {
    await pauseAllJobs();
    await load();
  }

  const active = jobs?.filter((j) => j.job.status === "active").length ?? 0;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-5 overflow-y-auto px-4 md:px-6 py-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-semibold text-xl">Tasks &amp; watches</h1>
          <p className="text-muted-foreground text-sm">
            Scheduled work runs unattended. Ask in chat (“watch this price every
            6 hours”) or add one here.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={pauseAll} disabled={active === 0}>
            <Pause />
            Pause all
          </Button>
          <Button onClick={() => setCreating((open) => !open)}>
            <Plus />
            New
          </Button>
        </div>
      </header>

      {creating && (
        <JobForm
          submitLabel="Create"
          onSubmit={create}
          onCancel={() => setCreating(false)}
        />
      )}
      {error && <p className="text-destructive text-sm">{error}</p>}

      {jobs && jobs.length === 0 && !creating && (
        <p className="rounded-lg border border-dashed p-8 text-center text-muted-foreground text-sm">
          Nothing scheduled yet.
        </p>
      )}

      {jobs && jobs.length > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full min-w-[36rem] text-sm">
            <thead className="bg-muted/50 text-left text-muted-foreground text-xs">
              <tr>
                <th className="px-3 py-2 font-normal">Name</th>
                <th className="px-3 py-2 font-normal">Schedule</th>
                <th className="px-3 py-2 font-normal">Next run</th>
                <th className="px-3 py-2 font-normal">Last run</th>
                <th className="px-3 py-2 font-normal">Trend</th>
                <th className="px-3 py-2 font-normal">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {jobs.map(({ job, last_run, recent_values, alert_count }) => {
                const condition = job.spec.watch?.condition;
                const threshold =
                  condition &&
                  (condition.kind === "below" || condition.kind === "above")
                    ? condition.threshold
                    : undefined;
                return (
                  <tr key={job.id} className="hover:bg-muted/30">
                    <td className="px-3 py-2">
                      <Link
                        href={`/tasks/${job.id}`}
                        className="font-medium underline-offset-4 hover:underline"
                      >
                        {job.spec.name}
                      </Link>
                      <div className="text-muted-foreground text-xs">
                        {job.spec.kind}
                        {alert_count > 0 && (
                          <span className="ml-2 inline-flex items-center gap-0.5 text-amber-700 dark:text-amber-400">
                            <Bell className="size-3" />
                            {alert_count}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {describeSchedule(job.spec.schedule)}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {formatWhen(job.next_run_at)}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {last_run ? (
                        <span
                          className={
                            last_run.status === "failed"
                              ? "text-destructive"
                              : undefined
                          }
                        >
                          {last_run.status} · {formatWhen(last_run.created_at)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {job.spec.kind === "watch" ? (
                        <Sparkline
                          values={recent_values}
                          threshold={threshold}
                        />
                      ) : (
                        <span className="text-muted-foreground text-xs">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {STATUS_LABELS[job.status]}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
