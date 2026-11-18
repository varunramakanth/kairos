"use client";

import { Check, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { judgeCalibration, labelVerdict, reviewQueue } from "@/client/sdk.gen";
import type { CriterionCalibration, ReviewItem } from "@/client/types.gen";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";

/**
 * Label judge verdicts one at a time. The judge's own verdict stays hidden until you
 * answer, so it cannot anchor your label.
 */
export default function ReviewPage() {
  const [queue, setQueue] = useState<ReviewItem[] | null>(null);
  const [answered, setAnswered] = useState<boolean | null>(null);
  const [calibration, setCalibration] = useState<CriterionCalibration[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [items, report] = await Promise.all([
      reviewQueue({ query: { limit: 20 } }),
      judgeCalibration(),
    ]);
    if (items.data) setQueue(items.data);
    else setError(errorMessage(items.error));
    if (report.data) setCalibration(report.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const item = queue?.[0];

  async function label(passed: boolean) {
    if (!item) return;
    const { error: failure } = await labelVerdict({
      body: {
        run_id: item.run_id,
        example_id: item.example_id,
        metric_id: item.metric_id,
        passed,
      },
    });
    if (failure) setError(errorMessage(failure));
    else setAnswered(passed);
  }

  async function next() {
    setAnswered(null);
    setQueue((q) => (q ? q.slice(1) : q));
    if (queue && queue.length <= 1) await load();
    else {
      const report = await judgeCalibration();
      if (report.data) setCalibration(report.data);
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl space-y-5 overflow-y-auto px-4 md:px-6 py-6">
      <header className="space-y-1">
        <Link
          href="/evals"
          className="text-muted-foreground text-xs underline-offset-4 hover:underline"
        >
          ← Evals
        </Link>
        <h1 className="font-semibold text-xl">Review the judge</h1>
        <p className="text-muted-foreground text-sm">
          Read the question and the answer, then say whether the answer meets
          the criterion. The judge&apos;s verdict is shown after you decide. A
          criterion is trusted once you and the judge agree well enough (kappa
          of at least 0.6 over 10 or more labels).
        </p>
      </header>
      {error && <p className="text-destructive text-sm">{error}</p>}

      {queue && !item && (
        <p className="rounded-lg border p-4 text-muted-foreground text-sm">
          Nothing to review. Run{" "}
          <code className="rounded bg-muted px-1">just evals-report</code> to
          publish judge verdicts.
        </p>
      )}

      {item && (
        <section className="space-y-3 rounded-lg border p-4">
          <p className="text-muted-foreground text-xs">
            {item.suite} · {item.example_id} · {queue?.length} left
          </p>
          <div className="rounded-md bg-primary/5 p-3 text-sm">
            <span className="font-medium">Criterion ({item.metric_id}): </span>
            {item.criterion}
          </div>
          <div>
            <h2 className="mb-1 text-muted-foreground text-xs">
              The user asked
            </h2>
            <p className="text-sm">{item.question}</p>
          </div>
          <div>
            <h2 className="mb-1 text-muted-foreground text-xs">
              Kairos answered
            </h2>
            <div className="max-h-96 overflow-y-auto rounded-md border p-3 text-sm">
              {item.answer ? (
                <Markdown text={item.answer} />
              ) : (
                <em>(no answer text)</em>
              )}
            </div>
          </div>

          {answered === null ? (
            <div className="flex gap-2">
              <Button onClick={() => label(true)}>
                <Check className="size-4" /> Meets it
              </Button>
              <Button variant="outline" onClick={() => label(false)}>
                <X className="size-4" /> Does not
              </Button>
              <Button variant="ghost" onClick={next}>
                Skip
              </Button>
            </div>
          ) : (
            <div className="space-y-2">
              <p
                className={`rounded-md p-3 text-sm ${answered === item.judge_passed ? "bg-emerald-500/10" : "bg-destructive/10"}`}
              >
                <span className="font-medium">
                  The judge {item.judge_passed ? "passed" : "failed"} it
                  {item.judge_score ? ` (${item.judge_score}/5)` : ""}
                  {answered === item.judge_passed
                    ? ": you agree."
                    : ": you disagree."}
                </span>{" "}
                {item.judge_reason}
              </p>
              <Button onClick={next}>Next</Button>
            </div>
          )}
        </section>
      )}

      {calibration.length > 0 && (
        <section className="space-y-2">
          <h2 className="font-medium">Calibration so far</h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[32rem] text-sm">
              <thead className="text-left text-muted-foreground text-xs">
                <tr>
                  <th className="py-1 font-normal">Criterion</th>
                  <th className="py-1 font-normal">Labels</th>
                  <th className="py-1 font-normal">Agreement</th>
                  <th className="py-1 font-normal">Kappa</th>
                  <th className="py-1 font-normal">Trusted</th>
                </tr>
              </thead>
              <tbody>
                {calibration.map((c) => (
                  <tr key={c.metric_id}>
                    <td className="py-1 font-mono text-xs">{c.metric_id}</td>
                    <td className="py-1">{c.n}</td>
                    <td className="py-1">{(c.agreement * 100).toFixed(0)}%</td>
                    <td className="py-1">{c.kappa?.toFixed(2) ?? "–"}</td>
                    <td className="py-1">{c.trusted ? "yes" : "not yet"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
