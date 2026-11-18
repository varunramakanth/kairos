"use client";

import { X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

export const START_TOUR = "kairos:start-tour";
const SEEN_KEY = "kairos.tour-seen";

type Step = { target: string; title: string; body: string };

/** Three steps; each highlights `[data-tour=target]` when it is on screen. */
export const TOUR_STEPS: readonly Step[] = [
  {
    target: "composer",
    title: "Ask in plain words",
    body: "Search, compare, plan or remember something. Kairos shows each tool it uses and asks before anything with a side effect, like sending you an email.",
  },
  {
    target: "nav",
    title: "It keeps working while you're away",
    body: "Say “watch this fare every morning” and it becomes a task. Results and anything waiting for your yes land in Approvals, by email or push.",
  },
  {
    target: "search",
    title: "Find anything with ⌘K",
    body: "Jump to a thread, a page or the theme. Every run has a trace with its cost and eval scores under Evals.",
  },
];

function seen(): boolean {
  try {
    return window.localStorage.getItem(SEEN_KEY) === "1";
  } catch {
    return true; // no storage: never nag
  }
}

function markSeen(): void {
  try {
    window.localStorage.setItem(SEEN_KEY, "1");
  } catch {
    // ignore
  }
}

/** The first-visit tour. Replay it from the command palette or the Welcome page. */
export function Tour() {
  const router = useRouter();
  const [step, setStep] = useState<number | null>(null);

  useEffect(() => {
    if (!seen()) setStep(0);
    const start = () => setStep(0);
    window.addEventListener(START_TOUR, start);
    return () => window.removeEventListener(START_TOUR, start);
  }, []);

  const current = step === null ? null : TOUR_STEPS[step];

  // Highlight the step's target while it is shown.
  useEffect(() => {
    if (!current) return;
    const element = document.querySelector<HTMLElement>(
      `[data-tour="${current.target}"]`,
    );
    if (!element || element.offsetParent === null) return;
    element.classList.add("tour-highlight");
    return () => element.classList.remove("tour-highlight");
  }, [current]);

  const close = useCallback(() => {
    markSeen();
    setStep(null);
  }, []);

  useEffect(() => {
    if (step === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, close]);

  if (step === null || !current) return null;
  const last = step === TOUR_STEPS.length - 1;
  return (
    <div
      role="dialog"
      aria-modal="false"
      aria-labelledby="tour-title"
      className="fixed inset-x-3 bottom-3 z-50 mx-auto max-w-sm rounded-xl border bg-popover p-4 text-popover-foreground shadow-2xl md:right-6 md:bottom-6 md:left-auto md:mx-0"
    >
      <div className="flex items-start justify-between gap-2">
        <h2 id="tour-title" className="font-medium">
          {current.title}
        </h2>
        <button
          type="button"
          onClick={close}
          aria-label="Skip the tour"
          className="rounded p-1 text-muted-foreground hover:bg-muted"
        >
          <X className="size-4" />
        </button>
      </div>
      <p className="mt-1 text-muted-foreground text-sm">{current.body}</p>
      <div className="mt-3 flex items-center gap-2">
        <span className="text-muted-foreground text-xs tabular-nums">
          {step + 1} / {TOUR_STEPS.length}
        </span>
        {step > 0 && (
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto"
            onClick={() => setStep(step - 1)}
          >
            Back
          </Button>
        )}
        {last ? (
          <Button
            size="sm"
            onClick={() => {
              close();
              router.push("/welcome");
            }}
          >
            Open the setup checklist
          </Button>
        ) : (
          <Button
            size="sm"
            className={step > 0 ? "" : "ml-auto"}
            onClick={() => setStep(step + 1)}
          >
            Next
          </Button>
        )}
      </div>
    </div>
  );
}
