# Learning the Kairos harness

A guided tour of the agent runtime in this repository: what a harness is, how Kairos's loop works line by line, how it survives crashes, how it keeps the model safe from what it reads, and where subagents and MCP fit. It is written to be read top to bottom, with links into the code at each step.

Shorter reference pages: [harness.md](../harness.md) (the loop in one diagram), [architecture.md](../architecture.md) (the system map). The reasoning behind each choice is in [the ADRs](../adr/).

---

## 1. What a "harness" is

A model on its own can only turn text into text. An **agent** is a model in a loop: it asks for a tool, something runs the tool, the result goes back to the model, and this repeats until the model gives a final answer. The **harness** is everything around the model that makes the loop real:

| Harness job | The question it answers |
|---|---|
| The loop | When do we call the model again, and when do we stop? |
| Tools | What can the model do, with what inputs, and how is a call run and validated? |
| Context | What exactly does the model see on each call? |
| Control | Which tool calls run, which wait for a human, which never run? |
| Durability | What happens if the process dies halfway through a tool call? |
| Budgets | How much may one run spend in steps, tokens, dollars and seconds? |
| Observability | What happened, in a form that evals and dashboards can read? |

Kairos writes its own harness instead of using an agent framework ([ADR-001](../adr/001-own-harness.md)). Kairos is always on: runs are started by schedules and continue after crashes and deploys. That needs control over checkpoints, leases and side effects that frameworks don't expose. Owning the loop also makes it a good reference to learn from, since every behavior is in about 600 lines you can read.

> **No LangChain, by rule.** The project's `CLAUDE.md` allows the Claude Agent SDK, Strands and LangGraph only as *comparison adapters* for evals. None of those adapters has been built yet (see section 11).

---

## 2. The map: packages and seams

```
kairos_domain     Pydantic models only: messages, RunState, events, tool specs
kairos_harness    AgentLoop + the Protocols it depends on (the "seams")
kairos_providers  Anthropic API / Bedrock implementations of Provider
kairos_tools      @tool, ToolRegistry, web/flights/deals/plan tools, the MCP client
kairos_policy     Approval rules (PolicyHooks) and injection screening
kairos_memory     Memory tools, embeddings, reflection hook
kairos_scheduler  Jobs, watches, schedule tools
kairos_notify     Outbox, channels (console, SES email, Web Push), notify_me
kairos_db         Postgres: queue + leases, checkpointer, stores
kairos_telemetry  OpenTelemetry GenAI spans (TracingHooks)
services/worker   Wires everything together; runs the durable worker
services/browser  A headless browser exposed as an MCP server
```

The most important design idea is **the loop depends only on Protocols** ([protocols.py](../../packages/kairos_harness/src/kairos_harness/protocols.py)). It never imports the Anthropic SDK, Postgres or a real tool:

| Protocol | What it abstracts | Real implementation | Test or eval implementation |
|---|---|---|---|
| `Provider` | one streaming model call | `kairos_providers` (Anthropic, Bedrock) | `ReplayProvider` (cassettes), `ScriptedProvider` |
| `ToolExecutor` | the list of tools and how to run one | `ToolRegistry` | `ReplayToolExecutor`, fakes |
| `Checkpointer` | durable state, events and blobs | `PostgresCheckpointer` | `InMemoryCheckpointer` |
| `Clock` | time | `SystemClock` | `FakeClock` (evals use a fixed "now") |
| `IdGenerator` | ids | UUIDs | `SeededIdGenerator` (deterministic) |
| `Hooks` | observation and control points | policy, spend, audit, tracing | `BaseHooks` no-ops |
| `ContentScreen` | injection screening of untrusted output | `InjectionScreen` | none, or a fake |

Because of these seams the *same loop* runs in production, in unit tests and in the evals harness. Only the implementations plugged in differ. [wiring.py](../../services/worker/src/kairos_worker/wiring.py) is the one place that knows every concrete class.

---

## 3. The vocabulary

Read [kairos_domain/run.py](../../packages/kairos_domain/src/kairos_domain/run.py) alongside this section.

- **`RunRequest`** is the durable input: `run_id`, `input`, `model`, `system_prompt`, `budget`, `history` (earlier turns of the thread), `user_id` (the owner, from M10).
- **`RunState`** is everything needed to continue a run: `messages`, `step`, `pending_tool_calls`, `usage`, `cost_usd`, `epoch`, `status`, `stop_reason`, `next_seq`. The checkpointer saves this.
- **A step** is one model call plus the tool batch it asked for.
- **`ToolCallRecord`** is one requested call with a status: `pending`, then `running`, then a result. It becomes `uncertain` if the process died mid-call.
- **Events** ([events.py](../../packages/kairos_domain/src/kairos_domain/events.py)) come in two kinds:
  - **Durable events** have a `seq` number and are stored: `RunStarted`, `ToolCallEnd`, `ToolCallResult`, `ApprovalRequested`, `RunFinished`, and so on.
  - **Ephemeral events** are live streaming only: `TextMessageDelta`, `ToolCallArgs`.
- **`StopReason`** says why the loop stopped: `completed`, `awaiting_approval`, `awaiting_input`, `budget_exceeded`, `refused`, `failed`, `cancelled`. The two `awaiting_*` reasons are *pauses* (status `PAUSED`); the rest are endings.

---

## 4. The loop, line by line

Open [loop.py](../../packages/kairos_harness/src/kairos_harness/loop.py). `AgentLoop.run` is an async generator: it **yields events as they happen**, which is how the UI streams a run live.

### 4.1 Start or resume ([loop.py:151](../../packages/kairos_harness/src/kairos_harness/loop.py:151))

```python
state = await self._checkpointer.load(request.run_id)
if state is None:   # new run: history + the user's input
    ...yield RunStarted
else:               # a checkpoint exists: another process was running this
    state.attempt += 1
    ...yield RunResumed
    ...self._recover_interrupted_calls(session)   # the resume rule, section 5
```

The same entry point handles a new run and a resumed one. Running a finished run again does nothing. This makes "retry the job" safe for the queue.

### 4.2 The main loop ([loop.py:176](../../packages/kairos_harness/src/kairos_harness/loop.py:176))

```python
while state.stop_reason is None:
    if context.cancel.is_set(): finish(CANCELLED)
    phase = self._tool_phase if state.pending_tool_calls else self._model_phase
```

There are two phases, and **pending tool calls decide which one runs next**. Because that choice depends only on saved state, a resumed run continues in the right phase automatically.

### 4.3 Model phase ([loop.py:201](../../packages/kairos_harness/src/kairos_harness/loop.py:201))

1. **Check the budget** before spending anything (`exceeded(...)` in [budget.py](../../packages/kairos_harness/src/kairos_harness/budget.py)).
2. **Compact the context if needed** (section 6.4).
3. **Build the `ModelRequest`:** the system prompt, *all* messages, *all* tool specs, max tokens and effort. Then `hooks.before_model` may change it.
4. **Stream** ([`_stream_model`](../../packages/kairos_harness/src/kairos_harness/loop.py:282)):
   - Provider events become `TextMessageStart/Delta` and `ToolCallStart/Args` for the UI.
   - A retryable `ProviderError` is retried with exponential backoff. The UI first gets a `ModelStreamReset` so it can clear partial text.
   - *Only ephemeral events* are produced while streaming, so a failed attempt leaves nothing durable to undo.
5. **Record the result:**
   - durable `TextMessageEnd` and `ToolCallEnd` events, which carry the *complete* text and arguments;
   - the assistant message is appended to history;
   - usage and cost are added up, and a `ModelCallFinished` and a `BudgetUpdated` event are emitted.
6. **Branch on the stop reason:**
   - `refusal` → finish as `REFUSED`;
   - `max_tokens` → finish as `FAILED`. It never runs a truncated tool call: half a JSON object can still parse.
   - tool calls requested → save them as `pending_tool_calls` and checkpoint;
   - otherwise → `COMPLETED` with the text as the answer.

### 4.4 Tool phase ([loop.py:334](../../packages/kairos_harness/src/kairos_harness/loop.py:334))

This is the most important function in the repository. Its five numbered comments are the design:

1. **Uncertain calls block the batch.** A call left `uncertain` by a crash pauses the run as `AWAITING_INPUT`, until a human says what happened (`RunContext.resolutions`).
2. **Decide every call before running any.** Each call ends up in exactly one of three lists:
   - `settled`: denied by a human, an unknown tool, or denied by policy. These become error results the model can read.
   - `approvals`: the policy said `RequireApproval` and no human has approved it yet.
   - `runnable`: everything else.

   If *any* call needs approval, the **whole batch pauses**. Nothing from that step runs, so the user never ends up with half a step done. An `ApprovalRequested` event is emitted per call. If the model didn't explain itself, `pending_summary` writes a plain-language "Waiting for your approval…" from the call arguments ([context.py](../../packages/kairos_harness/src/kairos_harness/context.py)).
3. **Record intent, then checkpoint.** Runnable calls are marked `running` and saved *before* they run. This save is also the **lease guard**: if another worker now owns the run, `save` raises `LostLeaseError` and no side effect happens.
4. **Run the batch in parallel** with `asyncio.TaskGroup`. A tool that raises becomes an error result; it never cancels its siblings.
5. **Return all results in one user message,** in the order the model asked. Each result goes through `_present` (screening, truncation, untrusted wrapping, section 6). Then `after_tool` hooks run, and the loop checkpoints.

Then the loop goes back to step 4.2. With no pending calls left, the next phase is a model call.

### 4.5 Finishing ([loop.py:593](../../packages/kairos_harness/src/kairos_harness/loop.py:593))

`_finish` sets the stop reason and status (`PAUSED` for approval or input, `FINISHED` otherwise), emits `RunFinished` with usage and cost, checkpoints, and calls `on_complete`. Two exceptions are handled around the whole loop:

- **`LostLeaseError`:** another process owns the run. *Write nothing* and re-raise.
- **`BudgetStop`:** raised by a hook, such as the daily spend cap. It finishes as `BUDGET_EXCEEDED` with a message for the user.

---

## 5. Durability: crashes, leases and "no hidden retries"

Runs live in Postgres ([ADR-003](../adr/003-durable-queue.md), [queue.py](../../packages/kairos_db/src/kairos_db/queue.py)). A worker **claims** a run with a lease that has an expiry and a fencing token (`lease_id`). Every checkpoint write includes that `lease_id`. If the lease expired and another worker claimed the run, the old worker's next write fails. That's the `LostLeaseError` above.

When a new worker resumes a run, `_recover_interrupted_calls` ([loop.py:504](../../packages/kairos_harness/src/kairos_harness/loop.py:504)) applies **the resume rule** to any call saved as `running`:

| The tool is… | What happens | Why |
|---|---|---|
| `idempotent=True` (search, read a page) | Run it again with the same idempotency key (the tool call id) | Doing it twice is harmless |
| `idempotent=False` (send an email) | Mark it `uncertain` and pause the run for a human | We can't know whether the email went out, and a duplicate email is worse than a question |

This is one of the repository's firm rules: *a non-idempotent tool call with an unknown outcome pauses the run for a human.*

---

## 6. Context engineering: what the model sees

### 6.1 A system prompt that never changes ([context.py](../../packages/kairos_harness/src/kairos_harness/context.py))

`build_system_prompt` = identity + `DATA_POLICY`. It deliberately contains **nothing that changes**, not even the time, because the provider's *prompt cache* only hits when the beginning of the request is byte-for-byte the same as before. For the same reason, tools are registered **in a fixed order** (the comments in `wiring.py` say "tool order is part of the prompt cache"). The time is a tool (`current_time`) instead of a line in the prompt.

### 6.2 History is append-only

Messages are frozen Pydantic models, and the loop only ever appends. Thinking blocks travel as opaque blocks and are replayed unchanged. A thread's later turns pass earlier messages as `RunRequest.history` ([threads.py](../../packages/kairos_db/src/kairos_db/threads.py), `continuable_history`). Editing history would invalidate both the cache and the model's thinking blocks.

### 6.3 Untrusted content is data

Every tool declares `provenance`. Results from untrusted tools (web pages, search, flights, MCP tools) go through `_present` ([loop.py:541](../../packages/kairos_harness/src/kairos_harness/loop.py:541)):

1. **Screening** ([screening.py](../../packages/kairos_policy/src/kairos_policy/screening.py)):
   - It always removes invisible text (Unicode tag characters, long zero-width runs) and pattern-matches for text aimed at an AI ("ignore previous instructions", fake system messages).
   - Only on a match, a small model looks at the snippets around each match to decide.
   - Content flagged as suspect is *still shown to the model*, with a warning, so the agent can tell the user what the page tried. Screening never silently drops information, and a failure in screening never fails the run.
2. **Truncation:** results over 20,000 characters are cut. The full text is stored as a blob, and the model sees `[truncated: N more characters…]`.
3. **Wrapping:** `wrap_untrusted` puts the content inside `<untrusted_content source="…">`. It also escapes any `</untrusted_content` text the page itself contains, so a page **can't forge the closing tag** and "escape" into instructions.

The system prompt's `DATA_POLICY` tells the model what those tags mean.

### 6.4 Compaction ([compaction.py](../../packages/kairos_harness/src/kairos_harness/compaction.py))

When the estimated prompt goes over `compact_at_prompt_tokens` (150k by default):

1. The loop asks the model for a structured summary: goals, decisions with source URLs, work in progress, and things to avoid. The request is the run's exact prefix plus one instruction appended to the last user message, so it's served mostly from the cache.
2. The old messages are archived as a blob, `epoch` goes up by one, and history restarts as a single user message: the `<conversation_summary>`, followed by the user's latest words verbatim (so the request isn't paraphrased away).
3. A `ContextCompacted` event records it.

This is "compaction starts a new epoch" from the repository's rules: earlier turns are never edited, they're replaced *as a whole* and archived.

---

## 7. Tools

### 7.1 Defining one ([registry.py](../../packages/kairos_tools/src/kairos_tools/registry.py))

```python
class SearchInput(BaseModel):
    query: str = Field(description="What to search for")

@tool(risk=ToolRisk.READ, idempotent=True, provenance=Provenance.UNTRUSTED)
async def web_search(args: SearchInput, context: ToolContext) -> ToolOutput: ...
```

- The **input is a Pydantic model**. Its JSON Schema is what the model sees, and it validates every call. Invalid input is sent back to the model as an error it can fix. This matters because, with eager input streaming, the provider doesn't validate tool input for us.
- **`risk`** is `READ`, `WRITE_SELF` (changes Kairos data), `WRITE_EXTERNAL` (acts outside Kairos) or `FINANCIAL`. It drives the policy.
- **`idempotent`** drives the resume rule.
- **`provenance`** drives wrapping and screening.
- **`ToolContext`** carries `run_id`, `tool_call_id`, `idempotency_key` and **`user_id`**. Tools read the owner from the context (M10), so one shared tool set serves every user without changing the cached prompt.

### 7.2 What Kairos offers the model (registered in [wiring.py](../../services/worker/src/kairos_worker/wiring.py), in this order)

| Tool | Risk | Untrusted output? | Notes |
|---|---|---|---|
| `current_time` | read | | The time lives here so the system prompt stays the same |
| `update_plan` | read | | A visible checklist; feeds the `plan_completion` metric |
| `web_search`, `deals_search` | read | yes | Tavily ([ADR-008](../adr/008-web-search.md)) |
| `web_fetch` | read | yes | Own fetcher with an SSRF guard: checks each redirect hop, refuses private addresses |
| `flights_search` | read | yes | SerpApi Google Flights ([ADR-014](../adr/014-flights-deals-embeddings.md)) |
| `record_observation` | read | | Used only inside watch runs: the model *observes*, code decides whether to alert ([watch.py](../../packages/kairos_scheduler/src/kairos_scheduler/watch.py)) |
| `schedule_create` | write_self | | A *proposal*: the job is created inactive until the user confirms |
| `schedule_list`, `schedule_set_status` | read / write_self | | |
| `notify_me` | write_external | | Email or push. Always needs approval |
| `memory_recall` | read | | Vector search over this user's memories |
| `memory_remember`, `memory_forget` | write_self | | A remembered fact is a proposal the user confirms |
| `browser_*` | per tool | yes | From the MCP browser server (section 9.2) |
| `delegate` | read | yes | Starts a subagent (section 9.1); registered last |

---

## 8. Hooks: where control and observation plug in

`Hooks` has seven methods ([protocols.py](../../packages/kairos_harness/src/kairos_harness/protocols.py)): `on_run_start`, `before_model`, `after_model`, `before_tool`, `after_tool`, `on_error`, `on_complete`. Only two can change what the loop does, and they do it through their return value:

- `before_model` returns the (possibly changed) request;
- `before_tool` returns `Allow`, `Deny(reason)` or `RequireApproval(reason)`.

`HookChain` ([hooks.py](../../packages/kairos_harness/src/kairos_harness/hooks.py)) runs several hook sets in order. For `before_tool`, the **first decision that isn't `Allow` wins**. In production, [service.py:132](../../services/worker/src/kairos_worker/service.py:132) builds the chain in this order:

```
SpendCapHooks → PolicyHooks → AuditHooks → (ReflectionHooks) → TracingHooks
```

| Hook | Package | What it does |
|---|---|---|
| `SpendCapHooks` | worker | Raises `BudgetStop` when today's spend hits the overall cap or the user's own cap (M10); notifies once |
| `PolicyHooks` | policy | The approval rules below |
| `AuditHooks` | worker | Writes every side-effecting decision and result to `audit_log` |
| `ReflectionHooks` | memory | After a run, proposes memories from it. **Off in production** (`KAIROS_MEMORY_REFLECTION`) |
| `TracingHooks` | telemetry | OpenTelemetry GenAI spans. Last, so it sees the final state. This is what evals read |

**The policy rules** ([policy.py](../../packages/kairos_policy/src/kairos_policy/policy.py)), where the first rule that applies wins:

1. `FINANCIAL` → always **Deny**. Money is never moved by the agent, even with approval.
2. `READ` → **Allow**.
3. Kill switch on → **Deny** every side effect.
4. Proposal tools (`schedule_create`, `memory_remember`) → **Allow**, because the user confirms them elsewhere.
5. Everything else → **RequireApproval**. That's the owner's "strict" setting.

---

## 9. Have we built agents and MCP? Yes, three things

### 9.1 Subagents: an agent that starts agents ([subagents.py](../../packages/kairos_harness/src/kairos_harness/subagents.py), [ADR-013](../adr/013-subagents-and-memory.md))

The parent model can call `delegate(profile, task)`. That starts a **child run**: an ordinary `AgentLoop` with four limits.

- **Isolated context.** The child sees only the task text, never the parent's history. A long research detour doesn't fill the parent's context window.
- **Read-only tools.** `ReadOnlyTools` offers only the profile's allowed tools, *and only those with `READ` risk*. So a child never needs an approval, can't act on the user's behalf, and can't delegate further (`delegate` isn't in its tool list).
- **Its own budget.** By default: 12 steps, 16 tool calls, $0.30.
- **A compact result.** The parent gets the child's final answer (up to 6,000 characters) plus a usage line, not the child's transcript. That result is marked *untrusted*, because it was built from web content.

Today there is one profile, **`research`** ([wiring.py](../../services/worker/src/kairos_worker/wiring.py), `SUBAGENT_PROFILES`). It searches and reads the web (including the browser's read-only tools) and returns a short cited summary. The child runs with the same hook chain (policy, tracing) and screening as the parent. Its run id is `{parent}.sub.{tool_call_id}`, so its trace stays linked to the parent's.

> This is the "orchestrator plus specialist" multi-agent pattern from the AWS article, at its smallest: a parent that delegates research to one kind of child.

### 9.2 An MCP *client*: any MCP server becomes Kairos tools ([mcp.py](../../packages/kairos_tools/src/kairos_tools/mcp.py))

MCP (Model Context Protocol) is a standard way for a program to describe tools (name, description, JSON Schema) and run them. Kairos connects as a **client**: it starts each configured server as a subprocess over stdio, lists its tools, and registers each one as a normal Kairos `Tool`. Adding a capability is then a config entry (`McpServerConfig`, or `KAIROS_MCP_SERVERS`), not new code.

The key design rule: **trust is decided by Kairos, never by the server.**

- A server's `readOnlyHint` and similar annotations are ignored, because they're claims from code we don't control.
- An MCP tool with no entry in its server's config defaults to `write_external`, `untrusted`, non-idempotent. So it waits for approval, and its output is treated as data.
- `allow` limits which of a server's tools are offered at all.
- Tool names are prefixed with the server name (`browser_navigate`), so a server can't shadow a built-in tool or another server's tools.

### 9.3 An MCP *server*: the browser ([services/browser](../../services/browser/src/kairos_browser/server.py), [ADR-012](../adr/012-browser-and-mcp.md))

Kairos also *ships* an MCP server: a headless Chromium (Playwright) exposing `navigate`, `snapshot`, `extract`, `click`, `search`, `type` and `press`. The worker starts it through its own MCP client, `BROWSER_SERVER` in `wiring.py`, and sets the policy itself:

- `navigate`, `snapshot`, `extract`, `click`, `search` are `READ`;
- `type` and `press` are `WRITE_EXTERNAL`, so they need approval;
- every page is untrusted.

Two safety details worth studying:

- **A pinning proxy** ([proxy.py](../../services/browser/src/kairos_browser/proxy.py)). Chromium is forced through a local forward proxy. The proxy resolves each hostname *once*, refuses it unless every address is public, then connects to the exact address it checked. That defeats **DNS rebinding**: a name that answers "public" for the check and "private" for the connection can't reach your network. Only ports 80 and 443 are allowed.
- **Running it as a separate process** keeps a large dependency (Chromium) behind a narrow protocol. It could also be swapped for any other MCP browser.

---

## 10. Streaming to the UI

The worker yields events. Durable events are stored with their `seq` in `run_events`, then a Postgres `NOTIFY` fires when the transaction commits. Ephemeral deltas are sent by `NOTIFY` only. The API `LISTEN`s and streams Server-Sent Events whose `id` is the `seq`, so a browser that reconnects asks for events after its last `id` and loses nothing ([ADR-002](../adr/002-streaming-protocol.md), [ADR-007](../adr/007-live-events.md)). The UI's TypeScript types are generated from these same Pydantic event models (`just gen-client`).

---

## 11. Testing a harness without spending money

- **Cassettes at the provider boundary** ([cassette.py](../../packages/kairos_harness/src/kairos_harness/cassette.py), [ADR-005](../adr/005-cassettes.md)):
  - `RecordingProvider` stores each model call as one JSONL line: a fingerprint (hash) of the normalized request plus the events the model returned.
  - `ReplayProvider` serves them back. A request that isn't in the cassette raises `CassetteMissError`. It *never* falls back to the network.
  - Recording above the SDK means cassettes survive SDK upgrades and never contain credentials.
- **Tool cassettes** (`RecordingToolExecutor`, `ReplayToolExecutor`) do the same at the tool boundary, because the web changes between runs.
- **Fakes:** `FakeClock`, `SeededIdGenerator`, `InMemoryCheckpointer` and `ScriptedProvider` ([testing.py](../../packages/kairos_harness/src/kairos_harness/testing.py)) make runs fully deterministic.
- **The network is blocked:** `pytest-socket` allows loopback only.

**Not built yet:** comparison adapters that run the eval suites against the Claude Agent SDK, Strands or LangGraph. The `HarnessAdapter` protocol in `kairos_evals` is ready for them; see [the evals guide](evals-guide.md).

---

## 12. Follow one run end to end

Take *"Email me today's top Canadian business headlines"*:

1. **The API** inserts a queued run (`owner = you`) and starts streaming its events.
2. **The worker** claims it with a lease and calls `AgentLoop.run`. `RunStarted` goes out, then a checkpoint.
3. **Model phase:** the model asks for `web_search`, and the UI shows its arguments streaming in. The loop records `ToolCallEnd` and checkpoints.
4. **Tool phase:** policy allows `web_search` (read). The call is marked `running`, the loop checkpoints (the lease guard), the search runs, and the result is screened and wrapped as untrusted.
5. **Model phase:** the model writes a summary and asks for `notify_me`.
6. **Tool phase:** policy says `RequireApproval` because `notify_me` acts outside Kairos. `ApprovalRequested` goes out, and the run **pauses** as `awaiting_approval`. Nothing has been sent.
7. **You approve in the UI.** The run is queued again with `RunContext.approved = {that call id}`. A worker resumes it from the checkpoint, the email goes into the outbox, and the dispatcher delivers it to *your* verified address.
8. **Model phase:** "Sent." → `RunFinished(completed)`. Tracing closes the trace, and online evals score it (see the evals guide).

If the worker had died during step 7's send, the resume rule would mark `notify_me` as `uncertain` and ask you, instead of possibly emailing you twice.

---

## 13. Suggested reading order and exercises

1. [protocols.py](../../packages/kairos_harness/src/kairos_harness/protocols.py), then [loop.py](../../packages/kairos_harness/src/kairos_harness/loop.py). Read `_tool_phase` twice.
2. [context.py](../../packages/kairos_harness/src/kairos_harness/context.py) and [compaction.py](../../packages/kairos_harness/src/kairos_harness/compaction.py).
3. [registry.py](../../packages/kairos_tools/src/kairos_tools/registry.py) and one real tool, such as [web.py](../../packages/kairos_tools/src/kairos_tools/web.py).
4. [policy.py](../../packages/kairos_policy/src/kairos_policy/policy.py) and [hooks.py](../../packages/kairos_harness/src/kairos_harness/hooks.py).
5. [subagents.py](../../packages/kairos_harness/src/kairos_harness/subagents.py), [mcp.py](../../packages/kairos_tools/src/kairos_tools/mcp.py) and [the browser server](../../services/browser/src/kairos_browser/server.py).
6. [wiring.py](../../services/worker/src/kairos_worker/wiring.py) and [service.py](../../services/worker/src/kairos_worker/service.py) to see it all assembled.

Exercises:

- **Write a tool.** Add a `@tool` that returns a random quote. Register it in `build_tools`, and run `uv run kairos run "give me a quote"`. Then mark it `provenance=UNTRUSTED` and look at how the output reaches the model.
- **Break the batch rule.** In a unit test, have the model ask for `web_search` and `notify_me` in the same step. Check that the search *doesn't* run before approval, and work out why that's the right behavior.
- **Simulate a crash.** Save a checkpoint with a `running` non-idempotent call, resume it, and watch for `ToolCallUncertain`. The existing tests in `packages/kairos_harness/tests/` show how.
- **Add an MCP server.** Point `KAIROS_MCP_SERVERS` at any stdio MCP server, leave its tools unlisted, and confirm that every call asks for approval.
