"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { getEvalRun } from "@/client/sdk.gen";
import type { EvalRunDetail } from "@/client/types.gen";
import { errorMessage } from "@/lib/api";
import { formatMetric, gateFailures, outcomesByExample } from "@/lib/evals";
import { formatWhen } from "@/lib/jobs";

export function EvalRunView({
  params,
}: {
  params: Promise<{ evalRunId: string }>;
}) {
  const { evalRunId } = use(params);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showPassing, setShowPassing] = useState(false);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await getEvalRun({
        path: { eval_run_id: evalRunId },
      });
      if (data) setDetail(data);
      else setError(errorMessage(failure));
    })();
  }, [evalRunId]);

  if (error) return <p className="p-6 text-destructive text-sm">{error}</p>;
  if (!detail) return <p className="p-6 text-muted-foreground">Loading…</p>;

  const { run, results } = detail;
  const outcomes = outcomesByExample(results);
  const failing = outcomes.filter((o) => o.failures.length > 0);
  const shown = showPassing ? outcomes : failing;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-5 overflow-y-auto px-4 md:px-6 py-6">
      <header className="space-y-1">
        <Link
          href="/evals"
          className="text-muted-foreground text-xs underline-offset-4 hover:underline"
        >
          ← Evals
        </Link>
        <h1 className="font-semibold text-xl">{run.suite}</h1>
        <p className="text-muted-foreground text-sm">
          {run.model} · {run.harness} {run.harness_version} · {run.examples}{" "}
          cases · {formatWhen(run.started_at)}
          {run.git_sha ? ` · ${run.git_sha}` : ""}
        </p>
      </header>

      {gateFailures(run).length > 0 && (
        <ul className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-2 text-destructive text-sm">
          {gateFailures(run).map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2">
        {run.summary
          .filter((m) => m.n > 0)
          .map((m) => (
            <span
              key={m.metric_id}
              className="rounded border px-2 py-1 text-xs"
              title={formatMetric(m)}
            >
              <span className="font-mono">{m.metric_id}</span>{" "}
              <span className="tabular-nums">{m.mean.toFixed(2)}</span>
            </span>
          ))}
      </div>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">
            {failing.length} of {outcomes.length} cases with a failing metric
          </h2>
          <label className="flex items-center gap-1.5 text-muted-foreground text-xs">
            <input
              type="checkbox"
              checked={showPassing}
              onChange={(e) => setShowPassing(e.target.checked)}
            />
            show passing cases
          </label>
        </div>
        <ul className="divide-y rounded-lg border">
          {shown.map((outcome) => (
            <li key={outcome.exampleId} className="space-y-1 px-4 py-2">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-sm">{outcome.exampleId}</span>
                <span className="text-muted-foreground text-xs">
                  {outcome.passed} passed
                  {outcome.failures.length > 0 &&
                    `, ${outcome.failures.length} failed`}
                </span>
              </div>
              {outcome.failures.map((f) => (
                <p key={f.metric_id} className="text-sm">
                  <span className="font-mono text-destructive text-xs">
                    {f.metric_id}
                  </span>{" "}
                  <span className="text-muted-foreground">{f.reason}</span>
                </p>
              ))}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
