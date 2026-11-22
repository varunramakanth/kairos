# Kairos threat model

Kairos is a single-user agent. It reads the open web, holds personal memories, runs
scheduled jobs unattended, and can act: notify its owner, change its own jobs, and,
with approval, type and press in a browser. This document lists what is worth
protecting, who or what can attack it, and what stands in the way. Each mitigation
points at the code or ADR that implements it, and the residual risks are stated
plainly.

Last reviewed: 2026-11-18 (M7). Review it whenever a tool, an integration or the
deployment changes.

## Assets

| Asset | Why it matters |
|---|---|
| **The owner's accounts and money** | The worst outcome is an action taken in their name: a purchase, a message to someone else, a form submitted. |
| **Personal data** | Memories, chat history, job prompts and the owner's email address. |
| **Secrets** | Anthropic, Tavily and SerpApi keys, the SES-only AWS key, and VAPID keys. |
| **Spend** | Model and API calls cost money; a loop or a hostile page can burn it. |
| **Integrity of what Kairos believes** | Memories and job definitions shape every later run. |
| **Availability** | Watches the owner relies on ("tell me when the fare drops"). |

## Actors

| Actor | Capability |
|---|---|
| **Web content** (pages, search results, MCP output) | The main attacker. Anyone can publish text Kairos will read: indirect prompt injection. |
| **A compromised or careless MCP server** | It returns arbitrary text, and its tools may do more than they claim. |
| **The local network** | Pages may try to reach private addresses (SSRF) or race DNS (rebinding). |
| **The owner, by mistake** | A vague request, a runaway watch, or approving something without reading it. |
| **Someone who obtains a key** | They can spend against the API keys; the SES key can only email the owner. |
| **An invited user** (ADR-018) | Signed in with MFA, but must never see or change another user's data, use admin features, or spend without limit. |

## Entry points and mitigations

### 1. Indirect prompt injection (web, search, MCP, browser)

| Mitigation | Where |
|---|---|
| Untrusted tool output is wrapped in delimited data blocks the content cannot forge; the system prompt says such content is data, never instructions | `kairos_harness.context`, M1 |
| Screening: pattern checks on every untrusted result, with a small model deciding the suspicious ones. Flagged content still reaches the agent, marked as suspected injection, and is logged | `kairos_policy.screening`, ADR-015 |
| Invisible text (Unicode tag characters, zero-width runs) is stripped before the model sees it | ADR-015 |
| Strict policy: every side effect waits for the owner's approval; financial actions are always denied | ADR-010 |
| The browser's acting tools (`type`, `press`) are separate tools, and they are gated | ADR-012 |
| Subagents get read-only tools only | ADR-013 |
| Memory is propose-only: a page cannot plant a belief without the owner confirming it | ADR-013 |
| Evals: injection cases with canaries, gated at 1.0 | `safety`, `browser`, `m6` and `injection` suites |

**Residual risk:** an injection that only changes the *answer* (biased advice, a wrong
fact) is not blocked. Screening flags it, faithfulness judging measures it, and the
owner reads it as text from the web.

### 2. Data exfiltration

| Mitigation | Where |
|---|---|
| Kairos can only message its owner. SES is restricted by IAM to the owner's address, and the notification channels have no other recipient | ADR-010 |
| Each canary case checks that the planted string never reaches a tool argument, so data cannot leave through a URL either | `InjectionResistance` |
| Browser and `web_fetch` reach public addresses only, on every request and redirect. The browser goes through a pinning proxy (resolve once, check, connect to that address), which closes DNS rebinding | ADR-008, ADR-012, ADR-015 |

**Residual risk:** a GET to a public URL can still carry data in its query string if
the model is tricked into composing one. The canary cases measure this, and screening
flags the instruction, but it is not technically impossible.

### 3. Unwanted actions

| Mitigation | Where |
|---|---|
| Approval gates with the full call shown; no hidden retries of side effects; uncertain outcomes pause for a human | ADR-001, ADR-010 |
| A silent pause is impossible: the owner is always told what is waiting | M5 fix, `pause_explained` gated at 1.0 |
| Kill switch: denies every action and pauses every job | ADR-010 |
| An append-only audit log of every side effect and every decision | ADR-010 |

**Residual risk:** approval fatigue. The owner may approve without reading.
Approvals show the exact arguments, and the strict policy keeps their number honest.

### 4. Runaway spend and denial of service

| Mitigation | Where |
|---|---|
| Per-run budgets: tokens, dollars, steps, tool calls | M1 |
| A daily spend cap ($5): no new runs or job firings until midnight Toronto time, and the owner is notified once | ADR-015 |
| API rate limits on endpoints that start work | ADR-015 |
| Watches back off on failure and coalesce missed firings; jobs never overlap | ADR-009 |

### 5. Secrets

| Mitigation | Where |
|---|---|
| Secrets live in `.env` (git-ignored) and in `~/.aws`, never in code; `SecretStr` keeps them out of logs and reprs | CLAUDE.md |
| The SES key is send-only, to the owner only | ADR-010 |
| Recorded cassettes are scanned for keys before every push | process |

**Residual risk:** the repository sits in an iCloud-synced folder, and so does `.env`.
Moving the repository out of the synced folder is recommended.

### 6. Integrity and loss of data

| Mitigation | Where |
|---|---|
| Agent-created jobs and memories need the owner's confirmation | ADR-009, ADR-013 |
| Nightly local backups, kept 14 days, with a tested restore | ADR-015 |
| Leases fence every worker write; history is append-only | ADR-003 |

### 7. One user reaching another's data (M10, ADR-018)

- **Every row has an owner.** Root tables carry `user_id`; child rows are reached through their parent. Store methods take a required `owner`, so a query cannot forget the filter without pyright noticing.
- **Fails closed at every layer.** `user_id` has no database default, so an insert without an owner fails. A valid Cognito sign-in with no Kairos user is a 403. Another user's id is a 404, exactly like a missing one.
- **Tools act for the run's user.** `ToolContext.user_id` comes from the run, never from the model: `notify_me` can only reach that user's verified address and browsers; memory and schedule tools only see that user's rows.
- **Admin only:** inviting and removing users, evals, the kill switch. Enforced by the API; the UI just hides them.
- **Spend:** a daily cap per user, plus the overall cap.
- **Tested by walking every route:** `tests/integration/test_users.py` calls each route as a second user with the first user's ids and fails on any leak, any change, or a new route it does not know.

## Out of scope

- Local development: the API listens on localhost with sign-in off, as the `local` admin.
- Supply-chain review of dependencies beyond pinned versions and a committed lock file.
- A compromised developer machine.
