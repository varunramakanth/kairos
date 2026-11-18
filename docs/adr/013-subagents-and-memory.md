# ADR-013: Read-only subagents, and memory the user confirms

- **Status:** Accepted (2026-11-18; built before the embedding choice, which stays open)
- **Milestone:** M6

## Subagents

**Decision:** a `delegate` tool starts a child `AgentLoop` on a named profile
(`research` for now). The child:

- **Isolated context:** it sees only the task text, so a long research detour does
  not fill the parent's context window.
- **Read-only toolset:** it gets the profile's tools that are `read` risk, and nothing
  else, even if a profile lists more. So it never needs an approval, cannot act on the
  user's behalf, and cannot delegate further (`delegate` is not in any profile).
- **Own budget:** steps, tool calls and dollars are capped per profile. A child that
  runs out says so in its result.
- **Compact result:** the parent gets the child's final answer (up to 6,000
  characters) with a one-line usage note. The result is marked untrusted, since it is
  built from web content.

It runs inside the parent's tool call, so `delegate` is idempotent: a resumed parent
simply runs the child again. Child spans carry `kairos.parent_run_id`, and the child
uses `KAIROS_SUBAGENT_MODEL` (Sonnet 5.5 by default, per PLAN.md's routing table).

**Not done:** the child's cost is reported in its result but not added to the parent's
budget; the child's own cap bounds it. Delegation metrics (appropriateness,
communication) are in the deferred eval list.

## Memory

**Decision: the agent can only propose.**

- **Proposals:** `memory_remember` and the after-run reflection hook store *proposals*.
  A proposal is never recalled until the user confirms it on the Memory page, the same
  rule as agent-created jobs. This is the defence against memory poisoning: a planted
  instruction on a web page cannot become a "fact" the agent relies on in every future
  conversation.
- **Your own memories:** memories the user writes are active at once.
- **Allowed without a prompt:** `memory_remember` is in the policy's proposal tools,
  because it changes nothing the agent will act on.
- **Forgetting needs approval:** `memory_forget` is `write_self`, so under the strict
  policy it waits for approval. The user can always delete memories in the UI, and
  every UI change is audited.

**Reflection:** off by default (`KAIROS_MEMORY_REFLECTION=1`).

- **What it reads:** after a completed chat turn, the job model reads only the user's
  message and the final answer, never tool output, and proposes up to three durable
  facts. It costs one small model call per turn.
- **When it's skipped:** for scheduled jobs and subagents. Any failure is logged and
  ignored.

**Retrieval:** Postgres full-text search over a generated `tsvector`, with a GIN index.

- **Ranking:** relevance is scaled by recency; a memory untouched for 90 days counts
  half.
- **Fallback:** a substring match catches odd or very short queries.
- **Semantic search comes later:** recall via pgvector arrives with an embedding model,
  which is still the owner's decision. Hybrid ranking will then combine the two.

**Recall is a tool, not automatic injection.** Putting memories into the system prompt
would break prompt caching on every change, and would change every recorded eval
request. Instead, the `memory_recall` tool's description tells the model to look up
preferences before personal answers. Recalled memories appear in the trace as tool
output, which is what the memory metrics (context-retrieval precision and recall) will
read.

## Consequences

- **Demo re-recorded:** the app's tool list gained the memory tools and `delegate`, so
  the demo cassette was re-recorded. It is scripted, so this was free.
- **Eval cassettes unchanged:** the eval adapter builds its tools without memory or
  subagents, so recorded eval cases are untouched. Cases that use these tools will be
  recorded when M6 live spend is approved.
- **CORS:** the API now allows `DELETE`. Deleting a memory is its first `DELETE`
  endpoint, and a preflight test covers it.
