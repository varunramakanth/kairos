"use client";

import { CheckCircle2, CircleSlash, XCircle } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { listEvalRuns } from "@/client/sdk.gen";
import type { EvalRunSummary } from "@/client/types.gen";
import { Sparkline } from "@/components/sparkline";
import { errorMessage } from "@/lib/api";
import {
  byLayer,
  formatMetric,
  gateFailures,
  LAYER_LABELS,
  latestRuns,
  trend,
} from "@/lib/evals";
import { formatWhen } from "@/lib/jobs";

export default function EvalsPage() {
  const [runs, setRuns] = useState<EvalRunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await listEvalRuns({
        query: { limit: 500 },
      });
      if (data) setRuns(data);
      else setError(errorMessage(failure));
    })();
  }, []);

  const latest = runs ? latestRuns(runs) : [];

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 overflow-y-auto px-6 py-6">
      <header>
        <h1 className="font-semibold text-xl">Evals</h1>
        <p className="text-muted-foreground text-sm">
          The latest run of each suite, scored on three layers: the final
          response, the agent&apos;s components, and the model. Trends show the
          metric across runs on the same model.
        </p>
        <Link
          href="/evals/review"
          className="text-primary text-sm underline-offset-4 hover:underline"
        >
          Review the judge (calibration) →
        </Link>
      </header>
      {error && <p className="text-destructive text-sm">{error}</p>}
      {runs && latest.length === 0 && (
        <p className="rounded-lg border p-4 text-muted-foreground text-sm">
          No eval runs yet. Run{" "}
          <code className="rounded bg-muted px-1">just evals-report</code> to
          write the offline suites here.
        </p>
      )}
      {latest.map((run) => (
        <SuiteCard key={run.id} run={run} all={runs ?? []} />
      ))}
    </div>
  );
}

function SuiteCard({
  run,
  all,
}: {
  run: EvalRunSummary;
  all: EvalRunSummary[];
}) {
  const failures = gateFailures(run);
  return (
    <section className="rounded-lg border">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b px-4 py-3">
        <h2 className="font-medium">
          <Link
            href={`/evals/${run.id}`}
            className="underline-offset-4 hover:underline"
          >
            {run.suite}
          </Link>
        </h2>
        <GateBadge passed={run.passed} />
        <span className="text-muted-foreground text-xs">
          {run.model} · {run.harness} {run.harness_version} · {run.examples}{" "}
          cases · {formatWhen(run.started_at)}
          {run.git_sha ? ` · ${run.git_sha}` : ""}
        </span>
      </div>
      {failures.length > 0 && (
        <ul className="border-b bg-destructive/5 px-4 py-2 text-destructive text-xs">
          {failures.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
      <div className="divide-y">
        {byLayer(run.summary).map(([layer, metrics]) => (
          <div key={layer} className="px-4 py-3">
            <h3 className="mb-2 text-muted-foreground text-xs">
              {LAYER_LABELS[layer]}
            </h3>
            <table className="w-full text-sm">
              <tbody>
                {metrics.map((m) => (
                  <tr key={m.metric_id}>
                    <td className="py-1 pr-3 font-mono text-xs">
                      {m.metric_id}
                      {m.kind === "llm_judge" && (
                        <span className="ml-1.5 rounded bg-primary/10 px-1 text-primary">
                          judge
                        </span>
                      )}
                    </td>
                    <td className="w-16 py-1 pr-3 text-right tabular-nums">
                      {m.mean.toFixed(2)}
                    </td>
                    <td className="py-1 pr-3 text-muted-foreground text-xs">
                      {formatMetric(m)} · n={m.n}
                    </td>
                    <td className="w-32 py-1">
                      <Sparkline
                        values={trend(all, run.suite, run.model, m.metric_id)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </section>
  );
}

function GateBadge({ passed }: { passed: boolean | null | undefined }) {
  if (passed == null)
    return (
      <span className="flex items-center gap-1 text-muted-foreground text-xs">
        <CircleSlash className="size-3.5" /> not gated
      </span>
    );
  return passed ? (
    <span className="flex items-center gap-1 text-emerald-700 text-xs dark:text-emerald-400">
      <CheckCircle2 className="size-3.5" /> gate passed
    </span>
  ) : (
    <span className="flex items-center gap-1 text-destructive text-xs">
      <XCircle className="size-3.5" /> gate failed
    </span>
  );
}
