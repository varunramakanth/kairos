# ADR-010: Strict approvals, an audit log, and a notification outbox

- **Status:** Accepted (2026-11-17; choices made by the owner)
- **Milestone:** M4

## Decisions

### 1. Strict policy, enforced in a `before_tool` hook

`kairos_policy.Policy` decides every tool call; the first rule that applies wins:

1. `FINANCIAL` risk: denied, always. Approval cannot override it.
2. `READ`: allowed.
3. Kill switch on: every other call is denied.
4. Proposal tools (`schedule_create`, whose job does nothing until the user confirms it): allowed.
5. Everything else waits for approval, including email to the owner and job changes. This is the owner's *strict* choice.

A `Deny` beats a human approval, so turning the kill switch on also stops calls that were approved but have not run yet.

### 2. Approvals pause the run; decisions resume it

- **Pausing:** the loop emits `approval_requested` (with the input and risk) and pauses as `awaiting_approval`. The checkpointer records an `approvals` row, an "approval needed" notification and an audit entry in the same transaction as the event.
- **Deciding:** a decision updates the row. When nothing else is pending for that run, the run is queued again.
- **Resuming:** the worker builds a `RunContext` with `approved` and `denied` ids from the table. Approved calls run; declined calls return "The user declined this action" plus the user's note, so the model can adapt.
- **Scheduled jobs:** a job run that pauses for approval stays in flight, so the job does not fire again until the user decides.

### 3. Audit log

`audit_log` is append-only. It records:

- Executed or failed side-effect tool calls (from an `after_tool` hook).
- Approval requests and decisions.
- Job changes made in the UI.
- Kill switch flips.
- Notifications sent.

### 4. Notifications go through an outbox

Producers never send directly. An alert, an approval request, a digest or a `notify_me` call inserts a `notifications` row in the same transaction as its cause. The dispatcher claims due rows with `SKIP LOCKED`, sends each requested channel, and records a result per channel.

- **Retries:** a failed channel is retried with backoff, up to 5 attempts. Channels that already succeeded are not sent again.
- **Crashes:** a crash between sending and recording can repeat a message, but cannot lose one.
- **Dedupe:** `dedupe_key` makes producers idempotent. `notify_me` uses its tool call's idempotency key, so a retried call cannot send twice.

Channels:

| Channel | How | Notes |
|---|---|---|
| console | structured log | always on |
| email | Amazon SES API v2 with a send-only IAM policy (owner's choice, revised 2026-11-17) | the recipient is fixed to `KAIROS_EMAIL_TO`; plain SMTP remains as an option |
| push | Web Push with VAPID, self-hosted keys (`kairos vapid-keys`) | expired subscriptions (404/410) are dropped |

System notifications (alerts, the digest) are delivered without approval. Approval applies to actions the *agent* chooses to take.

### 5. The weekly digest is data, not a model call

A built-in job (`job_weekly_digest`, Sunday 18:00 America/Toronto) builds the digest in code. It covers the week's alerts, each job's runs and failures with its latest observation, pending approvals, run count and spend. It is created once at worker start; the owner can pause or reschedule it like any job. A digest firing records a job run with no agent run.

### 6. Email through SES, not a Gmail app password (revised)

The owner first chose Gmail SMTP with an app password, then switched to SES before any credential was created. A Gmail app password grants full mailbox access over IMAP and SMTP and bypasses 2-Step Verification; Kairos only needs to send to one address. The SES credentials are limited by IAM to `ses:SendEmail` on one verified identity, with `ses:FromAddress` and `ses:Recipients` conditions, and come from an AWS SSO profile rather than a long-lived key in `.env`. Setup steps: [`docs/ses-setup.md`](../ses-setup.md).

## Consequences

- An unattended task that wants to act blocks until the owner decides. That is the intended cost of the strict setting.
- The approval card in chat and the inbox are the only places an action can be approved. The agent has no tool for it, and the API is the human's.
- The 8000-byte `NOTIFY` limit does not apply here; notifications are rows, not `NOTIFY` payloads.
