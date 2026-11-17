"use client";

import { Activity, Info } from "lucide-react";
import Link from "next/link";
import type { RunDetail } from "@/client/types.gen";
import { Markdown } from "@/components/markdown";
import { PlanChecklist } from "@/components/plan-checklist";
import { RunApprovals } from "@/components/run-approvals";
import { ScheduleProposal } from "@/components/schedule-proposal";
import { ToolCard } from "@/components/tool-card";
import { useRunStream } from "@/hooks/use-run-stream";
import { PLAN_TOOL } from "@/lib/run-view";

const ACTIVE = new Set(["queued", "running"]);

const STOP_LABELS: Record<string, string> = {
  completed: "",
  budget_exceeded: "Stopped: budget exceeded",
  awaiting_approval: "Paused: waiting for approval",
  awaiting_input: "Paused: needs your input",
  cancelled: "Cancelled",
  refused: "The model declined this request",
  failed: "Failed",
};

/** One turn: the user's message and everything the agent did to answer it. */
export function Turn({
  detail,
  onFinished,
}: {
  detail: RunDetail;
  onFinished?: () => void;
}) {
  const { run } = detail;
  const view = useRunStream(
    run.id,
    detail.events,
    ACTIVE.has(run.status),
    onFinished,
  );
  const working = !view.finished && ACTIVE.has(run.status);
  const stop = view.finished?.stop_reason ?? run.stop_reason;
  const error = view.finished?.error ?? run.error;

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-primary px-4 py-2 text-primary-foreground">
          {run.input}
        </div>
      </div>
      <div className="space-y-2">
        {view.plan && view.plan.length > 0 && (
          <PlanChecklist plan={view.plan} />
        )}
        {view.items.map((item) => {
          if (item.kind === "text") {
            return (
              <div
                key={item.id}
                className={item.streaming ? "streaming-cursor" : undefined}
              >
                <Markdown text={item.text} />
              </div>
            );
          }
          if (item.kind === "tool") {
            if (item.call.name === PLAN_TOOL) return null;
            if (
              item.call.name === "schedule_create" &&
              item.call.status === "done"
            ) {
              return <ScheduleProposal key={item.call.id} call={item.call} />;
            }
            return <ToolCard key={item.call.id} call={item.call} />;
          }
          return (
            <p
              key={item.id}
              className="flex items-center gap-2 text-muted-foreground text-xs"
            >
              <Info className="size-3.5" />
              {item.text}
            </p>
          );
        })}
        {working && view.items.length === 0 && (
          <p className="animate-pulse text-muted-foreground text-sm">
            Thinking…
          </p>
        )}
        {!working && stop === "awaiting_approval" && (
          <RunApprovals runId={run.id} onAllDecided={onFinished} />
        )}
        {stop && STOP_LABELS[stop] && (
          <p className="text-destructive text-sm">
            {STOP_LABELS[stop]}
            {error ? `: ${error}` : ""}
          </p>
        )}
        {!working && (
          <div className="flex items-center gap-3 text-muted-foreground text-xs">
            <span className="tabular-nums">${view.costUsd.toFixed(4)}</span>
            <Link
              href={`/runs/${run.id}`}
              className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
            >
              <Activity className="size-3" />
              Trace
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
