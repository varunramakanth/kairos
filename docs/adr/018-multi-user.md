# ADR-018: Multi-user: invited users with private data

- **Status:** Accepted (2026-11-21; invites, spend caps and email chosen by the owner)
- **Milestone:** M10
- **Code:** `kairos_db/users.py`, migration `0009_users`, `kairos_api/users.py`, `apps/web/src/app/users`

## Context

Kairos was built for one person. Cognito proved *who* signed in, but no table recorded an owner, so a second login would see and change everything: chats, memories, tasks, approvals, the audit log. Every email went to the owner.

The owner wants to invite a few people, each with private data. The owner's choices:

- **Invites:** from an in-app admin page.
- **Spend:** a daily cap per user, plus the existing overall cap.
- **Email:** each invitee verifies their address with SES; the account stays in the SES sandbox.

## Decision

### Identity

A `users` table keyed by the Cognito `sub`:

| Column | Meaning |
|---|---|
| `id` | The Cognito `sub` (or `local` when sign-in is off) |
| `email` | Where email notifications go |
| `role` | `admin` or `member` |
| `daily_cap_usd` | This user's cap; `NULL` means no cap of their own |
| `email_verified` | SES may send to this address |
| `disabled` | Signed-in requests are refused |

A request is resolved to a user by the `current_user` dependency:

- **Sign-in off** (local development, the demo): always the `local` admin.
- **Sign-in on:** the access token's `sub` must match a row that is not disabled; otherwise **403 "not invited"**.

**Bootstrap.** Migration `0009` creates the `local` admin and gives it every existing row. In production, the first time an unknown `sub` signs in while `local` still exists, the API asks Cognito who it is (`/oauth2/userInfo`). If the email is the configured owner (`KAIROS_EMAIL_TO`), the `local` row is renamed to that `sub`. Every foreign key is `ON UPDATE CASCADE`, so the owner's data moves with it in one statement. Anyone else is refused.

### Ownership

- `user_id` (foreign key to `users`) on the **root** tables: `threads`, `runs`, `jobs`, `memories`, `audit_log`, `notifications`, `push_subscriptions`.
- **Child** rows are reached through their parent, and checked through it: `run_events`, `run_steps`, `tool_calls`, `tool_result_blobs` and `approvals` through `runs`; `job_runs`, `watch_observations` and `alerts` through `jobs`.
- A run carries its owner in `RunRequest.user_id`; the loop copies it into `ToolContext.user_id`. Tools are still built once per worker, so the tool list, the prompt cache and the cassette fingerprints do not change. Tools that touch user data (memory, schedules, `notify_me`) read the owner from the context.

### Scoping fails closed

- Every store method that reads or changes user data takes a **required** keyword `owner`. There is no default, so pyright flags a call that forgets it.
- Another user's row looks exactly like a missing one: **404**, never 403, so ids cannot be probed.
- A test walks every route in the OpenAPI schema, creates data as one user and calls the route as another. A new route that is not covered fails the test.

### Roles

**Admin only:** the Users page and its API, evals and labelling (they describe the system, not a person), and the kill switch (it stays global and still pauses every job). Members manage their own chats, tasks, memories, approvals and notifications.

### Spend

`SpendLedger.spent_today` sums run costs for one user or for everyone. Before every model call, and before a run is admitted, both caps are checked: the user's own and the overall one.

### Notifications

A notification belongs to a user. The dispatcher looks up the recipient: email goes to `users.email` only when `email_verified`; push goes to that user's subscriptions only. Each user has their own weekly digest.

### Invites

`POST /users` calls Cognito `AdminCreateUser`, which emails a temporary password, and SES `CreateEmailIdentity`, which emails a verification link. Both go through `boto3` in a thread, behind a `Directory` protocol with a fake for tests. The task's IAM role gets those actions on the user pool and on SES identities only. `ses:SendEmail` is widened from "the owner only" to any verified identity, still sent from the owner's address; the SES sandbox itself refuses unverified recipients.

## Alternatives considered

| Option | Why not |
|---|---|
| A separate deploy per person | Simple isolation, but about $59 a month each and an AWS account to run each time |
| Postgres row-level security | A strong second layer, but the worker serves every user from one connection pool, so each transaction would need `SET LOCAL`. Possible later; the route-walking test covers the risk for now |
| A `user_id` column on every child table too | More indexes and more to keep in sync; parents already decide access |
| Members use the Cognito console | No code, but the owner wanted invites in the app |

## Consequences

- Every query in `kairos_db` that touches user data now has an owner filter, and the API passes it from `current_user`.
- The SES sandbox stays: an invitee gets no email until they click AWS's verification link. Push and the in-app list work immediately.
- Not in scope: a time zone per user (it stays global), a model key per user, SES production access, self sign-up.
