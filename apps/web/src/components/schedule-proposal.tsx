"use client";

import { CalendarClock, Check } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { confirmJob, getJob, updateJob } from "@/client/sdk.gen";
import type { JobStatus } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { proposedJobId, STATUS_LABELS } from "@/lib/jobs";
import type { ToolCallView } from "@/lib/run-view";

/**
 * A job the agent proposed with `schedule_create`. Nothing runs until the user
 * presses Confirm here (or on the Tasks page).
 */
export function ScheduleProposal({ call }: { call: ToolCallView }) {
  const jobId = proposedJobId(call.output);
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    void getJob({ path: { job_id: jobId } }).then(({ data }) =>
      setStatus(data?.job.status ?? "deleted"),
    );
  }, [jobId]);

  if (call.status !== "done" || !jobId) return null;

  async function confirm() {
    if (!jobId) return;
    const { data, error: failure } = await confirmJob({
      path: { job_id: jobId },
    });
    if (data) setStatus(data.status);
    else setError(errorMessage(failure));
  }

  async function discard() {
    if (!jobId) return;
    const { data } = await updateJob({
      path: { job_id: jobId },
      body: { status: "deleted" },
    });
    if (data) setStatus(data.status);
  }

  const lines = (call.output ?? "")
    .split("\n")
    .filter((line) => !line.startsWith("It is NOT active"));

  return (
    <section className="space-y-2 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <CalendarClock className="size-4" />
        Proposed schedule
        {status && (
          <span className="font-normal text-muted-foreground text-xs">
            · {STATUS_LABELS[status]}
          </span>
        )}
      </div>
      <pre className="whitespace-pre-wrap font-sans text-sm">
        {lines.join("\n")}
      </pre>
      {status === "pending_confirmation" && (
        <div className="flex gap-2">
          <Button size="sm" onClick={confirm}>
            <Check />
            Confirm
          </Button>
          <Button size="sm" variant="ghost" onClick={discard}>
            Discard
          </Button>
        </div>
      )}
      {status && status !== "pending_confirmation" && status !== "deleted" && (
        <Link
          href={`/tasks/${jobId}`}
          className="text-primary text-xs underline-offset-4 hover:underline"
        >
          Open in Tasks
        </Link>
      )}
      {error && <p className="text-destructive text-xs">{error}</p>}
    </section>
  );
}
