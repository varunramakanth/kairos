# ADR-015: Hardening: injection screening, a spend cap, rate limits, backups, a pinning proxy

- **Status:** Accepted (2026-11-18; screening, cap, backups and spend chosen by the owner)
- **Milestone:** M7
- **Threat model:** [docs/threat-model.md](../threat-model.md)

## 1. Injection screening: patterns always, a small model on matches

A `ContentScreen` runs in the loop on every untrusted tool result, before it is
wrapped for the model (`kairos_policy.screening.InjectionScreen`).

1. **Clean:** invisible text is removed: Unicode tag characters, which can hide
   instructions no human sees, and long zero-width runs.
2. **Match:** patterns look for text addressed to an AI.
   - **Phrasings:** override phrasing, "note to AI assistants", "Assistants: do X",
     uppercase `SYSTEM:`, "your assistant must", tool names, concealment
     ("do not tell the user"), rule suspension, and exfiltration verbs.
   - **What it matches against:** the text with Markdown escaping undone (`notify\_me`),
     plus any base64 runs, decoded.
3. **Decide:** only on a match, the job model (Haiku 5.5) reads the surrounding
   snippets, not the whole page, and decides whether the text instructs the assistant.
   An article about prompt injection is cleared.
   - **Unreadable verdict:** treated as suspected, erring on the side of a warning.

**Flagged content still reaches the agent.**

- **How it is marked:** it is preceded by a warning and marked
  `suspected_injection="true"`.
- **Where the flag shows:** on the event and on the trace span.
- **Why it is not dropped:** dropping content would hide what the page tried, and the
  agent should tell the user. Approval gates remain the hard stop.

**Byte-for-byte for clean content:** clean content is presented exactly as before, so
prompt caching and recorded cassettes are unaffected. Screening failures never fail a
run.

**Tuning on real data:**

- **Missed attacks:** the M7 injection cases found four (escaped tool names, three
  phrasings, base64). The patterns were widened.
- **False positives on real search results:** "camera system: 50 MP" and a login
  form's "Email or phone number … Password". They were removed, with regression tests.

## 2. Daily spend cap ($5) and rate limits

- **Daily spend:** summed from `run_steps` (one row per model call, with its cost) for
  the owner's local day. Screening and reflection calls are small and not counted.
- **Mid-run stop:** a hook checks the cap before every model call. Over the cap it
  raises the new `BudgetStop`, which the loop turns into a clean `budget_exceeded`
  stop with the reason, and the owner is notified once per day.
- **Refusing new work:** the API refuses new chat turns and job runs over the cap
  (429, with the reason and the reset time).
- **Rate limits:** endpoints that start work allow 20 requests a minute per client, with
  a sliding window and a 429 with `Retry-After`. That guards against a stuck client or
  script; edge protection is M8.
- **Settings page:** shows today's spend against the cap.

## 3. Nightly local backups

- **Commands:** `kairos backup` and `kairos restore` (`just backup` / `just restore`).
- **How it dumps:** `pg_dump -Fc` runs inside the Postgres container, so no client
  tools are needed on the host.
- **Where and how long:** files go atomically into `~/Backups/kairos`, outside the
  iCloud-synced repository, and are kept 14 days; the newest is never pruned.
- **Nightly:** the worker takes the day's backup after 03:00 local and catches up after
  sleep, without changing any system scheduler.
- **Restore test:** an integration test dumps the real test database, deletes data,
  restores it and finds the data again.
- **In AWS (M8):** RDS or Aurora snapshots replace this.

## 4. The browser goes through a pinning proxy

- **The gap (ADR-012):** the browser checked a page's address, then Chromium resolved
  the name again when it connected, so a DNS-rebinding attack could race the check.
- **The fix:** Chromium now sends every request, loopback included
  (`--proxy-bypass-list=<-loopback>`), through a local proxy in the browser service.
  The proxy resolves each host once, refuses unless every address is public, and
  connects to the address it checked.
- **What it accepts:** CONNECT for HTTPS, absolute-form for HTTP, and ports 80 and 443
  only.
- **Test:** a name answers public addresses for the browser's checks and a private one
  for the connection, and the proxy refuses it.

## Consequences

- **Runtime cost:** about 1 ms of patterns per untrusted result, and a fraction of a
  cent of Haiku only when patterns match.
- **Settings:** each piece has a setting: `KAIROS_INJECTION_SCREENING`,
  `KAIROS_DAILY_SPEND_CAP_USD`, `KAIROS_RATE_LIMIT_PER_MINUTE`, `KAIROS_BACKUPS`,
  `KAIROS_BACKUP_DIR` and `KAIROS_BACKUP_KEEP_DAYS`.
- **Residual risks:** listed in the threat model. They are injections that bias an
  answer without any action, data in a GET query string, and approval fatigue.
