/** Shaping eval runs for the dashboard. Pure functions, unit-tested. */
import type {
  EvalMetricSummary,
  EvalResultRow,
  EvalRunSummary,
} from "@/client/types.gen";

export const LAYERS = ["outcome", "component", "model"] as const;
export type Layer = (typeof LAYERS)[number];

export const LAYER_LABELS: Record<Layer, string> = {
  outcome: "Layer 1 · Final response",
  component: "Layer 2 · Agent components",
  model: "Layer 3 · Model",
};

/** The newest run of each suite and model, suites in a stable order. */
export function latestRuns(runs: readonly EvalRunSummary[]): EvalRunSummary[] {
  const latest = new Map<string, EvalRunSummary>();
  for (const run of runs) {
    const key = `${run.suite}\u0000${run.model}`;
    const seen = latest.get(key);
    if (!seen || run.started_at > seen.started_at) latest.set(key, run);
  }
  return [...latest.values()].sort(
    (a, b) => a.suite.localeCompare(b.suite) || a.model.localeCompare(b.model),
  );
}

export function byLayer(
  summary: readonly EvalMetricSummary[],
): [Layer, EvalMetricSummary[]][] {
  return LAYERS.map(
    (layer) =>
      [layer, summary.filter((s) => s.layer === layer && s.n > 0)] as [
        Layer,
        EvalMetricSummary[],
      ],
  ).filter(([, metrics]) => metrics.length > 0);
}

/** One metric's mean over a suite's runs on one model, oldest first. */
export function trend(
  runs: readonly EvalRunSummary[],
  suite: string,
  model: string,
  metricId: string,
): number[] {
  return runs
    .filter((r) => r.suite === suite && r.model === model)
    .sort((a, b) => a.started_at.localeCompare(b.started_at))
    .flatMap((r) => {
      const metric = r.summary.find((s) => s.metric_id === metricId);
      return metric && metric.n > 0 ? [metric.mean] : [];
    });
}

/** Mean, pass rate and, for measurements, the p50/p95 behind them. */
export function formatMetric(metric: EvalMetricSummary): string {
  if (metric.p50 != null && metric.p95 != null) {
    const unit = metric.metric_id.startsWith("cost") ? "$" : "";
    const suffix = metric.metric_id.startsWith("latency") ? "s" : "";
    const fmt = (v: number) =>
      `${unit}${v < 0.1 ? v.toFixed(4) : v.toFixed(2)}${suffix}`;
    return `p50 ${fmt(metric.p50)} · p95 ${fmt(metric.p95)}`;
  }
  return `${(metric.pass_rate * 100).toFixed(0)}% pass`;
}

export interface ExampleOutcome {
  exampleId: string;
  failures: EvalResultRow[];
  passed: number;
}

/** Results grouped per example, failing examples first. */
export function outcomesByExample(
  results: readonly EvalResultRow[],
): ExampleOutcome[] {
  const groups = new Map<string, ExampleOutcome>();
  for (const result of results) {
    if (!result.applicable) continue;
    const id = result.example_id ?? result.trace_id;
    const group = groups.get(id) ?? { exampleId: id, failures: [], passed: 0 };
    if (result.passed) group.passed += 1;
    else group.failures.push(result);
    groups.set(id, group);
  }
  return [...groups.values()].sort(
    (a, b) =>
      b.failures.length - a.failures.length ||
      a.exampleId.localeCompare(b.exampleId),
  );
}

/** The gate's failure lines, if the run was gated. */
export function gateFailures(run: EvalRunSummary): string[] {
  const failures = run.gate.failures;
  return Array.isArray(failures)
    ? failures.filter((f): f is string => typeof f === "string")
    : [];
}
