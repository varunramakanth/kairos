/** Display helpers for jobs. Types come from the generated client. */
import type { Condition, JobStatus, Schedule } from "@/client/types.gen";

export const STATUS_LABELS: Record<JobStatus, string> = {
  pending_confirmation: "Needs confirmation",
  active: "Active",
  paused: "Paused",
  completed: "Completed",
  expired: "Expired",
  deleted: "Deleted",
};

export function describeSchedule(schedule: Schedule): string {
  switch (schedule.kind) {
    case "cron":
      return `cron ${schedule.expression}${schedule.timezone ? ` (${schedule.timezone})` : ""}`;
    case "interval": {
      const hours = Math.floor(schedule.every_seconds / 3600);
      const minutes = Math.floor((schedule.every_seconds % 3600) / 60);
      const parts = [
        hours ? `${hours}h` : "",
        minutes ? `${minutes}m` : "",
      ].filter(Boolean);
      return `every ${parts.join(" ") || `${schedule.every_seconds}s`}`;
    }
    case "one_shot":
      return `once, ${formatWhen(schedule.at)}`;
  }
}

export function describeCondition(condition: Condition): string {
  switch (condition.kind) {
    case "below":
      return `below ${condition.threshold}`;
    case "above":
      return `above ${condition.threshold}`;
    case "contains":
      return `contains “${condition.text}”`;
    case "changed":
      return condition.min_delta
        ? `changes by ≥ ${condition.min_delta}`
        : "changes";
    default:
      return "";
  }
}

/** Local date and time, e.g. "Wed, Nov 18, 8:00 AM". */
export function formatWhen(
  iso: string | null | undefined,
  timeZone?: string,
): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-CA", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone,
  }).format(new Date(iso));
}

/** The job id inside a `schedule_create` tool result, if any. */
export function proposedJobId(output: string | undefined): string | null {
  return output?.match(/\bjob_[0-9a-f]{20}\b/)?.[0] ?? null;
}
