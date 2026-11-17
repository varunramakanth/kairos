# ADR-007: Live run events through Postgres LISTEN/NOTIFY

- **Status:** Accepted (2026-11-17)
- **Milestone:** M2

## Context

Runs execute in the worker; browsers connect to the API. ADR-002 makes durable events rows in `run_events`, so the API can always serve them. Token deltas are ephemeral by design (ADR-002, M1): they are never stored, because the durable `*_end` event carries the full value. Something has to carry those deltas from the worker process to the API process, and wake the API when new durable events are committed, without adding infrastructure.

## Options

| Option | For | Against |
|---|---|---|
| **Postgres `LISTEN/NOTIFY`** (chosen) | Already running. A notice sent inside the checkpoint transaction is delivered exactly at commit, so a listener never sees a `seq` it cannot read yet. | Payloads must stay under 8000 bytes. Delivery is best effort (lost if the listener's connection drops). One extra round trip per published batch. |
| Redis pub/sub | Fast, common. | A second datastore locally and on AWS, for one feature. |
| Poll `run_events` only | Simplest. | No live typing (deltas are not stored); polling tightly costs queries, loosely costs latency. |
| Store deltas too | One path. | Thousands of rows per answer, all redundant with the `*_end` event. |

## Decision

- The checkpointer sends `{"run_id", "through_seq"}` on channel `kairos_run_events` inside the transaction that inserts events.
- The worker's `EventPublisher` queues ephemeral events and sends them in small batches from a background task, so the agent loop never waits on it. Oversized payloads and a full queue drop events; that is safe because the durable event that follows carries the full value.
- Each API process holds one `LISTEN` connection (`EventListener`) and fans notices out to per-run subscriber queues.
- The SSE stream subscribes first, then reads `run_events`, then waits for a notice or a 2-second timeout before reading again. Correctness depends only on the table; notices only reduce latency.

## Consequences

- Live typing works across processes with no new service, locally and on RDS.
- If the listener's connection drops, live deltas stop until it reconnects, but every durable event still arrives within the poll interval. Reconnecting the listener automatically is a follow-up.
- If throughput ever outgrows NOTIFY (many concurrent streams), the `EventPublisher` and `EventListener` seams are where Redis or a WebSocket gateway would go.
