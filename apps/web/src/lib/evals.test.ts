import { describe, expect, it } from "vitest";
import type {
  EvalMetricSummary,
  EvalResultRow,
  EvalRunSummary,
} from "@/client/types.gen";
import {
  byLayer,
  formatMetric,
  gateFailures,
  latestRuns,
  outcomesByExample,
  trend,
} from "@/lib/evals";

function metric(
  metric_id: string,
  mean: number,
  extra: Partial<EvalMetricSummary> = {},
): EvalMetricSummary {
  return {
    metric_id,
    layer: "component",
    kind: "deterministic",
    n: 4,
    mean,
    pass_rate: mean,
    ...extra,
  };
}

function run(
  id: string,
  suite: string,
  day: number,
  summary: EvalMetricSummary[],
  model = "claude-sonnet-5-5",
): EvalRunSummary {
  return {
    id,
    suite,
    harness: "kairos",
    harness_version: "0.1.0",
    model,
    git_sha: null,
    started_at: `2026-11-${String(day).padStart(2, "0")}T00:00:00Z`,
    finished_at: `2026-11-${String(day).padStart(2, "0")}T00:05:00Z`,
    examples: 4,
    passed: true,
    summary,
    gate: {},
  };
}

function row(
  example_id: string,
  metric_id: string,
  passed: boolean,
  applicable = true,
): EvalResultRow {
  return {
    example_id,
    trace_id: `t-${example_id}`,
    metric_id,
    layer: "component",
    kind: "deterministic",
    value: passed ? 1 : 0,
    passed,
    applicable,
    measurement: null,
    reason: passed ? "ok" : "wrong",
    details: {},
  };
}

describe("eval dashboard shaping", () => {
  const runs = [
    run("a", "scheduling", 16, [metric("schedule_correctness", 0.8)]),
    run("b", "scheduling", 18, [metric("schedule_correctness", 0.9)]),
    run("c", "scheduling", 17, [metric("schedule_correctness", 0.85)]),
    run("h", "scheduling", 18, [metric("schedule_correctness", 0.6)], "haiku"),
    run("i", "intent_tools", 18, [metric("intent_accuracy", 1)]),
  ];

  it("keeps the newest run per suite and model", () => {
    expect(latestRuns(runs).map((r) => r.id)).toEqual(["i", "b", "h"]);
  });

  it("builds a metric trend oldest first, per model", () => {
    expect(
      trend(runs, "scheduling", "claude-sonnet-5-5", "schedule_correctness"),
    ).toEqual([0.8, 0.85, 0.9]);
    expect(trend(runs, "scheduling", "haiku", "schedule_correctness")).toEqual([
      0.6,
    ]);
  });

  it("groups by layer and drops metrics that never applied", () => {
    const grouped = byLayer([
      metric("cost_per_task", 1, { layer: "outcome" }),
      metric("intent_accuracy", 1),
      metric("plan_completion", 1, { n: 0 }),
    ]);
    expect(grouped.map(([layer, ms]) => [layer, ms.length])).toEqual([
      ["outcome", 1],
      ["component", 1],
    ]);
  });

  it("formats pass rates and measurements", () => {
    expect(formatMetric(metric("intent_accuracy", 0.75))).toBe("75% pass");
    expect(
      formatMetric(metric("cost_per_task", 1, { p50: 0.0123, p95: 0.05 })),
    ).toBe("p50 $0.0123 · p95 $0.0500");
    expect(
      formatMetric(metric("latency_per_task", 1, { p50: 2.5, p95: 9.25 })),
    ).toBe("p50 2.50s · p95 9.25s");
  });

  it("puts failing examples first and skips not-applicable scores", () => {
    const outcomes = outcomesByExample([
      row("good", "intent_accuracy", true),
      row("bad", "intent_accuracy", false),
      row("bad", "tool_selection_accuracy", false),
      row("good", "plan_completion", false, false),
    ]);
    expect(
      outcomes.map((o) => [o.exampleId, o.failures.length, o.passed]),
    ).toEqual([
      ["bad", 2, 0],
      ["good", 0, 1],
    ]);
  });

  it("reads gate failures defensively", () => {
    const gated = run("g", "s", 18, []);
    expect(gateFailures({ ...gated, gate: { failures: ["x fell"] } })).toEqual([
      "x fell",
    ]);
    expect(gateFailures(gated)).toEqual([]);
  });
});
