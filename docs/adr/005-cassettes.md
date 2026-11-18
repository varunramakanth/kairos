# ADR-005: Record/replay cassettes at two levels

- **Status:** Accepted (2026-11-16)
- **Milestone:** M1

## Context

Tests and offline evals must be deterministic and must not touch the network. `PLAN.md` proposed `vcrpy` for everything. Two facts change that:

1. `anthropic` (since 1.0; 2.9 today) moved its HTTP layer from `httpx` to `httpx2`. Tools that patch `httpx` (`vcrpy`, `respx`, `pytest-httpx`) keep importing but silently stop seeing SDK requests unless `httpx2.alias_httpx()` runs before they load.
2. HTTP-level recordings of model calls are tied to SDK and API wire details (headers, SSE framing, beta flags). They break on SDK upgrades and are unreadable in review.

## Decision

Two cassette levels, each used where it fits.

| Level | Used for | Format | Why |
|---|---|---|---|
| **Provider boundary** | Model calls | JSONL: one normalized `ModelRequest` fingerprint plus the `ModelEvent` stream it produced | Independent of SDK and transport. Readable diffs. The same files drive tests and `kairos_evals` replay, and work for any provider. |
| **HTTP** | Tools that call plain HTTP APIs (search, fetch, flights) | `vcrpy` YAML | Those tools have no narrower seam, and the wire format is what we want to pin. |

## Design

- `RecordingProvider` wraps a real `Provider` and writes a cassette; `ReplayProvider` serves one. Both implement the `Provider` Protocol from ADR-001, so the harness cannot tell the difference.
- Requests are matched by a fingerprint of the normalized request (model, system, messages, tool names) with volatile fields removed. A miss is a test failure that prints the nearest recorded fingerprint, never a silent network call.
- `LIVE=1` switches to record mode. Without it, sockets are blocked by `pytest-socket` (loopback excepted), so a missing cassette cannot fall through to the network.
- Secrets are scrubbed at record time: auth headers for `vcrpy`; provider cassettes never contain credentials because they are recorded above the transport.
- An early pytest plugin calls `httpx2.alias_httpx()` so `vcrpy` also sees any SDK traffic in the few tests that want it.

## Consequences

- Provider cassettes do not catch wire-level regressions in the SDK. One `live` smoke test per provider covers that.
- Cassettes need re-recording when prompts or tool schemas change. The fingerprint mismatch message says which.
