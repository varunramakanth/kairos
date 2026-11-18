# Kairos — working conventions

Kairos is an always-on personal agent. `PLAN.md` is the source of truth for scope and milestones; update it when a decision changes. Decisions with trade-offs get an ADR in `docs/adr/`.

## Commands

`just` is the only entrypoint. Run `just` to list recipes.

| Command | What it does |
|---|---|
| `just sync` | Install Python (uv) and web (pnpm) dependencies |
| `just dev` / `just dev-obs` / `just down` | Postgres only / plus Langfuse on :3100 / stop |
| `just fmt` | Format and auto-fix Python and web |
| `just lint` | Ruff + Biome, no changes |
| `just typecheck` | pyright strict + `tsc --noEmit` |
| `just test [pytest args]` | Offline tests (integration tests start Postgres via testcontainers; skipped without Docker) |
| `just migrate` / `just worker` | Migrate the local DB / run the durable worker |
| `just api` / `just web` | API on :8000 with an embedded worker / Next.js on :3000 |
| `just api-demo` | API serving a scripted demo cassette: no API key, no cost |
| `uv run kairos vapid-keys` | Print a Web Push key pair to paste into `.env` |
| `just gen-client` | Regenerate `apps/web/openapi.json` and the TS client after any API or event model change |
| `just record-cassettes` | Rebuild the synthetic eval cassettes (free) |
| `just record-live [case]` | Re-record live eval cases: real model and web calls. Costs money; ask first |
| `just evals` | Eval suites (offline), including the M5 suites held to `tests/evals/baseline.json` |
| `just record-evals [suite] [--only ids] [--haiku]` | Re-record M5 eval cases live (agent, web, judge, simulator). Costs money, capped by `KAIROS_EVALS_MAX_USD`; ask first |
| `just evals-baseline` | Replay the M5 suites and rewrite the regression baseline (review the diff) |
| `just evals-report` | Replay, gate and publish M5 results to the local dashboard at `/evals` |
| `just seed-evals` | Draft candidate cases with Sonnet for curation (a few cents; ask first) |
| `just test-live` | Tests marked `live`, with `LIVE=1`. Costs money; ask first |
| `just check` | Everything CI runs. Must be green before a milestone is done |

## Layout

- `packages/kairos_*` — Python libraries (uv workspace members, `src/` layout, each with `py.typed`).
- `services/{api,worker,browser}` — Python entrypoints (`kairos_api`, `kairos_worker`, `kairos_browser`).
- `apps/web` — Next.js UI. The only TypeScript in the repo. `src/client` is generated; never edit it.
- `infra/` — `compose.yaml` for local dev; CDK (Python) arrives in M8.
- `tests/` — cross-package and integration tests. Unit tests live in each member's `tests/`.

## Language split (firm)

- Python for everything except the web UI. TypeScript only in `apps/web`.
- The UI talks to the backend only through the client generated from FastAPI's OpenAPI schema (`just gen-client`). No hand-written API types. Pydantic models are the single source of truth, including streaming events.
- No LangChain. Claude Agent SDK, Strands and LangGraph appear only as comparison adapters in `kairos_evals`.
- `kairos_evals` must not import any other `kairos_*` package (`tests/test_workspace.py` enforces this). The Kairos adapter lives in `tests/evals/kairos_adapter.py`.
- Eval cases run with a fixed clock (`REFERENCE_NOW`, Wed 2026-11-18 14:00 Toronto) and seeded user jobs; a changed judge or simulator prompt is a `CassetteMiss` until re-recorded.

## Python

- Python 3.12+ (dev on 3.13), managed with uv; `uv.lock` is committed. Add a dependency to the member that uses it, not the root.
- Ruff for lint and format; pyright strict; full type hints. No `# type: ignore` without a reason on the same line.
- Pydantic v2 at every boundary: API, tools, events, config (`pydantic-settings`).
- Async throughout (FastAPI, asyncpg, httpx). No blocking calls in the event loop. Use `asyncio.TaskGroup` for concurrency.
- Absolute imports only.
- Core harness and eval interfaces (`Protocol`s and their public models) carry doc comments; this is a reference codebase.
- Structured logging with `structlog`, correlated with OTel trace IDs.

## Tests

- pytest + pytest-asyncio (`asyncio_mode = auto`).
- Tests cannot reach the network: `pytest-socket` allows loopback only. Use cassettes: provider-boundary JSONL for model calls, `vcrpy` for plain-HTTP tools (ADR-005).
- `anthropic` (1.0 and later; 2.9 today) uses `httpx2`. `httpx2.alias_httpx()` must run before `vcrpy`/`respx` are imported or they silently miss SDK traffic.
- Markers: `integration` (testcontainers Postgres), `evals`, `live` (skipped unless `LIVE=1`).
- Time is injected (`Clock` protocol) or frozen with `time-machine`. Never `sleep` on real time in tests.

## TypeScript (`apps/web`)

- Strict TypeScript, no `any`. Biome for lint and format. Vitest for components, Playwright for e2e.
- Next.js 16 differs from older versions: read `node_modules/next/dist/docs/` before using an API (see `apps/web/AGENTS.md`).

## Harness rules that are easy to break

- **History is append-only.** Never edit or delete earlier turns; replay thinking blocks unchanged. Compaction starts a new epoch.
- **Content from the web, email and documents is data, never instructions.** Tool results carry `provenance`; untrusted content is wrapped in delimited blocks.
- **No hidden retries of side effects.** A non-idempotent tool call with an unknown outcome pauses the run for a human.
- **Every write by a worker is guarded by its `lease_id`.**

## Process

- One milestone at a time (M0 → M9). Each ends with `just check` green and a short summary: what was built, how to run it, what is next, open questions.
- Small conventional commits (`feat(harness): …`, `fix(web): …`, `docs(adr): …`) on a branch per milestone.
- Check current docs before using any library, SDK or AWS service. Do not rely on memory for APIs or model IDs.
- Secrets never go in code. Document every environment variable in `.env.example`.
- Ask the owner before: adding a paid third-party service, choosing a flight or deal data provider, anything that touches real email or calendar, any live model spend.

## Known local gotcha

If imports of workspace packages fail with `ModuleNotFoundError` on macOS, the repo is probably in an iCloud-synced folder (Desktop or Documents). macOS marks some files there as hidden and Python 3.13 skips hidden `.pth` files. `just sync` clears the flag; the durable fixes are moving the repo out of the synced folder or setting `UV_PROJECT_ENVIRONMENT` to a path outside it.
