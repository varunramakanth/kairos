# ADR-004: One monorepo with extractable boundaries

- **Status:** Accepted (2026-11-16)
- **Milestone:** M0

## Context

`PLAN.md` names three repositories: `kairos` (app), `kairos-evals` (framework-agnostic eval library) and `kairos-infra` (CDK). It also allows starting with one repo and splitting later.

## Decision

One repository for now:

- `packages/kairos_evals` is a normal uv workspace member that could be published as it stands.
- `infra/cdk` will hold the CDK app (M8). `infra/compose.yaml` holds local services.
- The Python side is a uv workspace (`packages/*`, `services/*`) with one lockfile. `apps/web` is a standalone pnpm project; there is no root `package.json`.

## The rule that keeps the split cheap

`kairos_evals` must not import, or declare a dependency on, any other `kairos_*` package. It consumes OpenTelemetry traces and talks to agents through its own adapter Protocol. `tests/test_workspace.py::test_kairos_evals_is_standalone` parses its imports and its `pyproject.toml` and fails CI on a violation.

The Kairos adapter for the eval framework therefore lives outside `kairos_evals` (in the worker or a small glue package), not inside it.

## Why not split now

- Harness and evals change together through M1 to M5; cross-repo changes would slow every milestone.
- One CI pipeline and one lockfile while the interfaces are still moving.

## When to split

When `kairos_evals` has a second consumer, or needs its own release cadence. At that point: `git filter-repo` the directory, publish it, and replace the workspace source with a version pin.
