# Kairos

An always-on personal agent: it chats, runs scheduled jobs and watches, acts through tools behind an approval gate, remembers, and records a trace of every run that can be inspected, replayed and evaluated.

**Status:** M4 (approvals and notifications). Every agent action with a side effect waits in an approval inbox; an audit log, a kill switch, email and browser-push notifications, and a weekly digest. On top of M3 (scheduled tasks and watches with alerts), M2 (chat with live streaming, tool cards, web search and fetch, a trace viewer) and the M1 harness (durable runs on Postgres, both providers, tracing). See [`docs/harness.md`](docs/harness.md). See [`PLAN.md`](PLAN.md) for the full spec and milestones, and [`docs/adr/`](docs/adr/) for the decisions made so far.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), [just](https://just.systems/), Node 24 with pnpm (`corepack enable pnpm`), Docker.

```bash
just sync      # install Python and web dependencies
just dev       # start Postgres + pgvector
just migrate   # create the tables
just check     # lint, typecheck, tests, evals
```

To chat, put `ANTHROPIC_API_KEY` (and optionally `TAVILY_API_KEY` for web search) in `.env`, then run these in two terminals:

```bash
just api       # API on http://localhost:8000, with an embedded worker
just web       # chat UI on http://localhost:3000
```

No key yet? `just api-demo` instead of `just api` serves a scripted conversation; click the "Plan a three-step comparison…" example.

`just dev-obs` also starts a local Langfuse on http://localhost:3100. Copy `.env.example` to `.env` to override local defaults.

## Evals

82 golden cases cover intent, tool use, scheduling, safety, the browser, memory and simulated multi-turn conversations. They are recorded once against the real model and web, then replayed offline for free. Results are scored on three layers (final response, agent components, model) and held to a committed baseline in CI.

```bash
just evals          # replay every suite against the regression gate
just evals-report   # publish the results to the dashboard at http://localhost:3000/evals
```

See [docs/evals.md](docs/evals.md) for the workflow, and [ADR-011](docs/adr/011-eval-framework.md) for the design.

## Layout

| Path | Contents |
|---|---|
| `packages/kairos_*` | Python libraries: harness, providers, tools, memory, scheduler, policy, notify, db, telemetry, domain, evals |
| `services/` | Python entrypoints: `api`, `worker`, `browser` |
| `apps/web` | Next.js UI (the only TypeScript) |
| `infra/` | Local compose file; AWS CDK later |
| `docs/` | [Architecture](docs/architecture.md), [evals](docs/evals.md) and ADRs |

## Acknowledgements

The durable task worker (SQL leases with a fencing token, lease-guarded checkpoints), stored action reviews, the token-protected Playwright worker and the test layout are modelled on [CopilotKit OpenFuse](https://github.com/CopilotKit/openfuse) (MIT). OpenFuse is TypeScript; Kairos ports the patterns into Python rather than copying code.

The eval framework follows the AWS article "Evaluating AI agents: Real-world lessons from building agentic systems at Amazon".

## License

MIT. See [`LICENSE`](LICENSE).
