import { Circle, CircleCheck, CircleDot } from "lucide-react";
import type { PlanItem } from "@/lib/run-view";

export function PlanChecklist({ plan }: { plan: PlanItem[] }) {
  const done = plan.filter((item) => item.status === "completed").length;
  return (
    <section
      className="rounded-lg border bg-muted/30 px-3 py-2 text-sm"
      aria-label="Plan"
    >
      <div className="mb-1 flex items-center justify-between text-muted-foreground text-xs">
        <span>Plan</span>
        <span className="tabular-nums">
          {done}/{plan.length}
        </span>
      </div>
      <ul className="space-y-1">
        {plan.map((item, index) => (
          // Steps have no ids; position is their identity within one plan.
          // biome-ignore lint/suspicious/noArrayIndexKey: see above
          <li key={index} className="flex items-start gap-2">
            {item.status === "completed" ? (
              <CircleCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
            ) : item.status === "in_progress" ? (
              <CircleDot className="mt-0.5 size-4 shrink-0 text-primary" />
            ) : (
              <Circle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            )}
            <span
              className={
                item.status === "completed"
                  ? "text-muted-foreground line-through"
                  : ""
              }
            >
              {item.step}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
