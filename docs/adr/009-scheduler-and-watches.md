# ADR-009: Scheduler and watches: the model observes, code decides

- **Status:** Accepted (2026-11-17)
- **Milestone:** M3

## Context

M3 adds recurring work: tasks (a prompt on a schedule) and watches (check a value, alert on a condition). They run unattended, so mistakes are expensive: a double-fired job, an alert storm, an alert triggered by a web page, or a schedule the user never agreed to.

## Decisions

### 1. Each firing is an ordinary agent run

The scheduler only queues a `RunRequest` (with `metadata.job_id`); the existing worker executes it. Traces, budgets, the trace viewer and crash recovery all apply unchanged. Each job run gets a default budget (`$0.50`, 20 tool calls, 15 steps) and runs on `KAIROS_JOB_MODEL` (Haiku by default) unless the job names a model.

### 2. Watches: the model observes, code decides

A watch run must call `record_observation` once (`found`, `value`, `unit`, `text`, `source_url`). Whether that is worth an alert is decided by `kairos_scheduler.watch.evaluate`, which is pure code:

- **Condition:** below or above a threshold, text contains, or changed by at least `min_delta`.
- **Dedupe key:** derived from what triggered the alert (`below:1420`). The same key as the last alert never alerts again; a new low does.
- **Cooldown:** a minimum time between alerts.
- **Backoff:** failed runs, including a run that records no observation, push the next run out by 5 min × 2ⁿ⁻¹, capped at 6 h.

The alternative, asking the model "should I alert?", was rejected: it cannot be unit-tested, costs a judgment per run, and a fetched page could argue its way into an alert. Here a page can at most misreport a value, and that is visible in the stored observation.

### 3. Agent-created jobs need the user's confirmation

`schedule_create` stores the job as `pending_confirmation` and returns the next three fire times in local time. Only the user activates it (the Confirm button in chat, or the Tasks page). The agent can pause, resume and delete jobs, but cannot activate an unconfirmed one. Natural-language times ("tomorrow 8am") are resolved by the model with `current_time` and confirmed back, instead of by a date-parsing library.

### 4. Daylight saving

Cron expressions run on the local wall clock of the job's IANA timezone. A time inside the spring-forward gap runs one hour later on the wall clock (never skipped). A time in the repeated fall-back hour runs once, at the first occurrence; this applies to every expression, so `*/15` skips the repeated hour rather than running those wall-clock times twice. Intervals count elapsed time.

### 5. Leases, with the scheduler's clock

Claiming and firing follow ADR-003 (`SKIP LOCKED`, a fresh `lease_id`, every write conditioned on it). Two differences from runs:

- **Time is passed in** from the scheduler's injected `Clock` rather than read from `now()` in SQL, so tests can drive the scheduler across DST changes. Clock skew between scheduler processes is bounded by the lease TTL.
- **One transaction per firing** writes the job run, the agent run and the next `next_run_at`. A unique `(job_id, scheduled_for)` constraint backs up the lease.

A job never has two runs in flight: a due job with a started run is skipped until that run is settled. Firings missed while the scheduler was down collapse into one catch-up run.

## Consequences

- Alerts are stored (`alerts` table) and shown on the Tasks page. Delivering them (email, push) is M4.
- A watch that needs a browser (JavaScript-heavy pages, logins) waits for the M6 browser service; until then it can only use `web_search` and `web_fetch`.
- The schedule-confirmation card in chat finds the job id in the tool's text result. A structured tool-result field would be cleaner; revisit when approvals (M4) add structured cards.
