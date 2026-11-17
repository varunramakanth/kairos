# Kairos

An always-on personal agent: it chats, runs scheduled jobs and watches, acts through tools behind an approval gate, remembers, and records a trace of every run that can be inspected, replayed and evaluated.

**Status:** M1 (harness core). The agent loop, both providers, durable runs on Postgres and tracing work; there is no API or UI yet. See [`docs/harness.md`](docs/harness.md). See [`PLAN.md`](PLAN.md) for the full spec and milestones, and [`docs/adr/`](docs/adr/) for the decisions made so far.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), [just](https://just.systems/), Node 24 with pnpm (`corepack enable pnpm`), Docker.

```bash
just sync      # install Python and web dependencies
just dev       # start Postgres + pgvector
just check     # lint, typecheck, tests, evals
just web       # placeholder UI on http://localhost:3000
```

`just dev-obs` also starts a local Langfuse on http://localhost:3100. Copy `.env.example` to `.env` to override local defaults.

## Layout

| Path | Contents |
|---|---|
| `packages/kairos_*` | Python libraries: harness, providers, tools, memory, scheduler, policy, notify, db, telemetry, domain, evals |
| `services/` | Python entrypoints: `api`, `worker`, `browser` |
| `apps/web` | Next.js UI (the only TypeScript) |
| `infra/` | Local compose file; AWS CDK later |
| `docs/` | [Architecture](docs/architecture.md) and ADRs |

## Acknowledgements

The durable task worker (SQL leases with a fencing token, lease-guarded checkpoints), stored action reviews, the token-protected Playwright worker and the test layout are modelled on [CopilotKit OpenFuse](https://github.com/CopilotKit/openfuse) (MIT). OpenFuse is TypeScript; Kairos ports the patterns into Python rather than copying code.

The eval framework follows the AWS article "Evaluating AI agents: Real-world lessons from building agentic systems at Amazon".

## License

MIT. See [`LICENSE`](LICENSE).
