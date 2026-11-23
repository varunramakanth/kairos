# Evals

`kairos_evals` is a framework-agnostic eval library: it scores OpenTelemetry traces, and
the Kairos harness is one adapter (`tests/evals/kairos_adapter.py`). Design decisions are
in [ADR-011](adr/011-eval-framework.md). For the full guide (synthetic data, the judge
and its calibration, cadence, and a checklist against the AWS agent-evals article), see
[learn/evals-guide.md](learn/evals-guide.md).

## The workflow

| Step | Here |
|---|---|
| Define inputs | Golden datasets in `tests/evals/datasets/*.jsonl` (LLM-seeded drafts in `seeds/`, curated by hand), run through the harness with a fixed clock |
| Evaluate | `Suite` = dataset + metrics; `run_suite` turns each case into a trace and scores it |
| Share results | Sinks: JSONL, Postgres (the `/evals` dashboard), S3 Parquet (M8) |
| Audit / monitor | Regression gate against `tests/evals/baseline.json` in CI; dashboard trends |

## Suites

| Suite | Cases | What it measures |
|---|---|---|
| `intent_tools` | 28 | Intent (chat, research, action, clarify), tool selection and arguments, plan completion, and the judge's verdict on goal success, correctness, faithfulness and helpfulness |
| `scheduling` | 30 | Natural language to schedules: cron, interval and one-shot schedules, other timezones, DST edges, expiries, watch conditions |
| `safety` | 10 | Approval gates on every side effect; six prompt injections planted in fixture pages, each with a canary |
| `conversations` | 5 | Multi-turn chats with a simulated user (busy, vague, adversarial), judged as a whole |
| `browser` | 4 | A JavaScript-rendered page, site search, and two injections that try to make the agent press or type on a page |
| `m6` | 5 | Memory recall and proposals, memory poisoning through a web page, deals, and a real flight search |
| `injection` | 8 | Hidden text, image-link exfiltration, JSON fields, base64, forged delimiters, multi-step exfiltration, concealment, and a benign article |
| `failures` | 5 | Recovery from failing tools (errors, timeouts, garbled output, rejected input) |

## Metrics, by layer

- **Layer 1, outcome:**
  - Task completion; cost and latency per task (p50/p95)
  - Budget adherence
  - Judge criteria: goal success, correctness, faithfulness, helpfulness
- **Layer 2, components:**
  - Intent and clarification
  - Tool selection, parameters, error rate and redundant calls
  - Plan completion
  - Approval-gate adherence and injection resistance
  - Schedule correctness, citation grounding
  - Conversation goal success, topic adherence and safety
- **Layer 3, model:** the same suites on another model. `just evals-report` publishes
  Sonnet 5.5 and Haiku 5.5 runs side by side.

## Judge calibration

Open `/evals/review` after `just evals-report` and label judge verdicts. The judge's own verdict is shown only after you answer. Each criterion is trusted once there are 10 or more labels with Cohen's kappa of at least 0.6 (see ADR-016).

## Everyday commands

```bash
just evals            # offline replay of every suite, held to the baseline (CI runs this)
just evals-report     # the same, published to the local dashboard: http://localhost:3000/evals
```

## Changing behaviour on purpose

1. Change the harness, prompt or tools.
2. Re-record the cases it affects. This costs money and is capped by
   `KAIROS_EVALS_MAX_USD`:

   ```bash
   just record-evals intent_tools --only laundry_now,tech_headlines_email
   ```

3. Refresh the baseline and review its diff in the PR:

   ```bash
   just evals-baseline
   ```

A changed judge or simulator prompt replays as a `CassetteMiss` until it is re-recorded.
It never silently calls a model.

## Adding a case

Add a line to the right dataset. Every field is optional except `id` and `input`:

- **`intent`, `expected_tools`, `allowed_tools`, `forbidden_tools`, `expected_tool_args`:**
  what a correct run asks for. Argument strings match case-insensitively as substrings.
- **`expected_schedule`:** the job kind, schedule (compared by firing times), condition
  and expiry.
- **`expected_stop_reason`:** set to `awaiting_approval` for anything with a side effect.
- **`reference_answer`, `rubric`:** for the judge.
- **`canary`, plus `metadata.fixture_pages`:** for injection cases.
- **`simulation`:** persona, goal and turn limit, for a multi-turn case.

Then record just that case with `just record-evals <suite> --only <id>`.

## Using the library elsewhere

```bash
uv run kairos-evals run my.module:suite_factory --harness my.module:adapter_factory \
    --out jsonl:.evals --gate baseline.json
```

An adapter implements `HarnessAdapter`: it has a name, version and model, takes
`run(example) -> Trace`, and provides `conversation(example)` for simulated cases. Any
agent that emits OpenTelemetry GenAI spans can be evaluated.
