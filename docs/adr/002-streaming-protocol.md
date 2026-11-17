# ADR-002: Typed SSE defined by Pydantic, with AG-UI-aligned vocabulary

- **Status:** Accepted (2026-11-16)
- **Milestone:** M1 (event models), M2 (HTTP endpoint and generated client)

## Context

The UI needs a live stream of what a run is doing: text deltas, tool calls, plan updates, approvals, budget and cost. Two options: adopt AG-UI (the protocol CopilotKit and OpenFuse use, with a Python SDK, `ag-ui-protocol`), or define our own typed Server-Sent Events stream.

A project rule constrains the choice: Pydantic models are the single source of truth for API and streaming types, and the UI uses only a client generated from FastAPI's OpenAPI schema.

## Decision

Define our own SSE stream. `RunEvent` is a Pydantic discriminated union in `kairos_domain`, exported into the OpenAPI schema. Event names and lifecycle follow AG-UI where AG-UI has an equivalent, so an AG-UI endpoint can be added later as a mapping layer without changing the harness.

## Why not AG-UI directly

| Concern | Detail |
|---|---|
| Source of truth | AG-UI ships its own event models. Using them makes a third-party package the schema authority, or forces a parallel set of our own models. |
| Coverage | Kairos needs events AG-UI has no slot for: approval requested and resolved, budget updates, checkpoint written, cost per step. They would travel as untyped custom events. |
| Maturity | `ag-ui-protocol` is at 0.1.x. |
| Main benefit unused | AG-UI's payoff is CopilotKit's ready-made React components. The Kairos UI is hand-built on shadcn/ui. |

## Design

- **Vocabulary (aligned):** `run_started`, `text_message_start/delta/end`, `tool_call_start/args/end`, `tool_call_result`, `state_delta`, `run_finished`, `run_error`.
- **Kairos additions:** `step_started/finished`, `approval_requested/resolved`, `budget_updated`, `checkpoint_written`, `plan_updated`.
- **Durability:** every event is a row in `run_events` with a monotonic `seq` per run. The SSE `id` is that `seq`. A client that reconnects sends `Last-Event-ID` and receives everything after it. Watching a background run after the fact uses the same code path as watching a live one.
- **Typing in OpenAPI:** FastAPI does not describe SSE payloads on its own. The `RunEvent` union is registered as a schema component and referenced from the stream route's `text/event-stream` response, so the generated TypeScript client gets the union type.

## Consequences

- No drop-in CopilotKit or other AG-UI clients until the mapping layer exists.
- One schema, one generator, and a fully typed stream in the UI.
- Reconnect and replay come from the database design, not from the transport.
