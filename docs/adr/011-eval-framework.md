# ADR-011: Eval framework v1: own metrics, cached judges, a baseline gate

- **Status:** Accepted (2026-11-18; dataset, judge, storage and spend chosen by the owner)
- **Milestone:** M5

## Context

PLAN.md section 4 asks for an eval framework modelled on the AWS agent-evaluation
workflow: define inputs, evaluate, share results, audit. It also asks for a
three-layer metric library, golden datasets, an LLM user simulator, S3 and a
dashboard, and a CI regression gate. `kairos_evals` must stay standalone: it may not
import any other `kairos_*` package (ADR-004).

## Decisions

### 1. Write the metrics; do not adopt Ragas or DeepEval

Both libraries were checked on 2026-11-18:

- **Ragas 0.4.4** requires `langchain`, `langchain-core`, `langchain-community` and
  `langchain_openai` as core dependencies. Kairos has a firm no-LangChain rule.
- **DeepEval 4.3.4** requires `posthog` (product analytics), `grpcio`, and pytest with
  four plugins at runtime. That is heavy for a library others are meant to import,
  and the analytics would need auditing.

Most Kairos metrics are application-specific anyway: schedule correctness, approval
gates, injection canaries, intent read from requested tools. Generic RAG metrics such
as faithfulness are one rubric criterion of our own judge. The `Metric` protocol
stays open, so a third-party metric can still be wrapped later.

### 2. The trace is the interface; requested calls are recorded

Metrics read OpenTelemetry GenAI spans only. To judge tool use properly, the trace
needs what the model *asked for*, not just what ran. A send held for approval never
executes, but it was still chosen. With content capture on (eval runs only), the
tracing hook writes:

- `kairos.requested_tool_calls` (JSON) on each chat span
- `gen_ai.tool.call.arguments` on each tool span

An agent that emits neither is still evaluated; tool metrics then fall back to
executed calls.

### 3. Deterministic first, judge where only judgment works

- **Deterministic metrics:** intent (inferred from the most committed tool
  requested), clarification, tool selection, parameters, errors and redundancy,
  approval-gate adherence, injection resistance (forbidden tools plus a planted
  canary), schedule correctness, plan completion, task completion, and cost and
  latency (p50/p95).
- **The scheduling oracle is independent:** it reimplements next-fire times with
  croniter and zoneinfo instead of calling `kairos_scheduler`, under the same DST
  rules as ADR-009. Cron schedules are compared by their next six firings, so
  `0 9 * * 1-5` equals `0 9 * * mon-fri`, and an interval equals a cron with
  matching gaps.
- **Judge:** Claude Sonnet 5.5 with structured JSON output and low effort. One call
  per run scores every applicable criterion: goal success, correctness against a
  reference, faithfulness to tool output, and helpfulness. Tool output is delimited
  as data, the same rule the agent follows.
- **Judge context:** the judge sees tool output up to the agent's own truncation
  limit. At a shorter cut-off, facts the agent read looked invented; the live pilot
  caught exactly that.
- **Conversation judge:** scores goal success, topic adherence and safety across a
  whole simulated conversation.

### 4. Everything a model says is a cassette

- **Agent:** model turns and tool results use the ADR-005 cassettes, one pair per run.
- **Judge and simulator:** `ModelCassette` keys each answer by a SHA-256 of model,
  system prompt, messages and schema.
- **Changed requests fail loudly:** a changed prompt is a `CassetteMiss`, never a
  silent live call.
- **Replays are frozen:** a replayed suite is free, offline and bit-for-bit repeatable.
- **Re-recording:** `just record-evals` re-records, with a spend cap
  (`KAIROS_EVALS_MAX_USD`).

### 5. Datasets: LLM-seeded, then curated

Sonnet drafted 88 candidates from the tool list and a fixed reference time (Wednesday
18 November 2026, 14:00 Toronto). The drafts are kept in `datasets/seeds/`. Curation
kept 73 cases:

- 28 intent/tool cases
- 30 scheduling cases
- 10 safety cases (6 prompt injections served from fixture pages)
- 5 simulated conversations (busy, vague and adversarial personas)

Curation fixed labels, pinned exact schedules and arguments, removed cases with no
single right answer, and added `allowed_tools` where a correct run may or may not
fetch after searching. Every case runs with a fixed clock and seeded user jobs.

### 6. Results: JSONL and Postgres now, S3 at M8

- **Sinks:** `JsonlSink`, `PostgresSink` (tables from migration `0005_evals`, read by
  `/evals` in the web app) and `S3ParquetSink`. The S3 sink partitions by suite,
  date and version, the way Athena expects. It is tested offline with moto and gets
  a bucket at M8.
- **Schema tests:** an integration test writes through the sink into the
  Alembic-created schema, so the two cannot drift.

### 7. CI gate: a committed baseline with hard floors

- **Baseline:** `tests/evals/baseline.json` holds each suite's metric means.
- **Regressions fail:** a PR fails when a metric drops more than 0.02 below its
  baseline, falls under a floor (approval-gate adherence and injection resistance
  must be 1.0), or disappears.
- **Not gated:** cost and latency, because replay latency is not real latency.
- **Changing the baseline:** re-record, run `just evals-baseline`, and review the
  `baseline.json` diff in the PR.

### 8. Layer 3 is a comparison, not a metric

The same suites run against another agent model; the first comparison is Haiku 5.5.
The dashboard shows each suite per model, side by side on quality, cost and latency.

## Consequences

- **Recordings are snapshots:** live web answers drift, so re-recording a research
  case can change its scores. The baseline diff makes that visible.
- **Approximate signals:** intent inference and the "ends with a question" clarify
  signal are heuristics. Disagreements are visible per case in the dashboard and
  refined as cases accumulate.
- **Judge not yet calibrated:** Cohen's kappa is implemented, but calibration against
  human labels (and the review queue that collects them) is M7. Until then, judge
  metrics are gated only on regression from their own baseline, not on absolute
  thresholds.
