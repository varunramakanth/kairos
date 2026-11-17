# Architecture

This page is the map. `PLAN.md` has the full spec; `docs/adr/` has the reasoning behind each choice.

## System

```mermaid
flowchart TD
  UI[Web UI / PWA<br/>apps/web] -->|typed SSE + REST<br/>generated client| API[services/api]
  API --> DB[(Postgres + pgvector)]
  API -->|enqueue run| DB
  SCHED[Scheduler loop<br/>services/worker] -->|enqueue due jobs| DB
  DB -->|claim with lease| W[Run executor<br/>services/worker]
  W --> H[kairos_harness]
  H --> P[kairos_providers<br/>Anthropic / Bedrock]
  H --> T[kairos_tools]
  T --> EXT[Web, browser service, Gmail, flights, MCP]
  H --> POL[kairos_policy]
  POL -->|needs approval| DB
  H --> MEM[kairos_memory]
  H --> N[kairos_notify]
  H --> OTEL[kairos_telemetry]
  OTEL --> OBS[Langfuse / CloudWatch]
  OTEL --> EVAL[kairos_evals]
```

## Package dependencies

Arrows point from a package to what it may import. `kairos_evals` and `kairos_telemetry` import no other Kairos package.

```mermaid
flowchart BT
  domain[kairos_domain]
  harness[kairos_harness] --> domain
  providers[kairos_providers] --> domain
  tools[kairos_tools] --> domain
  memory[kairos_memory] --> domain
  scheduler[kairos_scheduler] --> domain
  policy[kairos_policy] --> domain
  notify[kairos_notify] --> domain
  db[kairos_db] --> domain
  api[services/api] --> domain
  worker[services/worker] --> harness
  worker --> domain
  telemetry[kairos_telemetry]
  evals[kairos_evals]
```

The harness depends on Protocols it defines (`Provider`, `ToolExecutor`, `Checkpointer`), not on `kairos_providers`, `kairos_tools` or `kairos_db`. The worker wires the concrete implementations together.

## Life of a run

```mermaid
sequenceDiagram
  participant UI
  participant API
  participant DB as Postgres
  participant W as Worker
  participant M as Model
  UI->>API: POST message
  API->>DB: insert run (queued)
  UI->>API: GET run events (SSE)
  W->>DB: claim run (lease)
  loop until done, out of budget, or blocked
    W->>M: stream model call
    M-->>W: text + tool calls
    W->>W: run tools in parallel
    W->>DB: append run_events, checkpoint (lease-guarded)
    DB-->>API: new events
    API-->>UI: SSE
  end
  W->>DB: finish run, release lease
```

## Decisions

| ADR | Decision |
|---|---|
| [001](adr/001-own-harness.md) | Own async harness behind a `Harness` Protocol; SDKs are comparison adapters |
| [002](adr/002-streaming-protocol.md) | Typed SSE from Pydantic models, AG-UI-aligned names |
| [003](adr/003-durable-queue.md) | Postgres leases with a fencing token, written by hand |
| [004](adr/004-repository-layout.md) | One monorepo; `kairos_evals` stays extractable |
| [005](adr/005-cassettes.md) | Cassettes at the provider boundary and at HTTP level |
