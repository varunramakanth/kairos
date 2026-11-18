"""``kairos-evals``: run suites, gate them against a baseline, and report.

Suites and harnesses are named by import path (``package.module:factory``), so the
CLI evaluates any agent without importing it itself. Each factory takes ``live``:
``False`` replays recorded cassettes, ``True`` calls real models and costs money.

    kairos-evals run tests.evals.suites:scheduling --harness tests.evals.kairos_adapter:replay \\
        --out jsonl:.evals --out postgres:postgresql+asyncpg://... --gate tests/evals/baseline.json
"""

import asyncio
import importlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

import typer

from kairos_evals.gate import Baseline, check
from kairos_evals.protocols import ResultSink
from kairos_evals.runner import HarnessAdapter, Suite, run_suite
from kairos_evals.types import EvalRun

app = typer.Typer(help="Run and gate agent eval suites.", no_args_is_help=True)


def load(path: str) -> Any:
    """Import ``package.module:attribute``."""
    module, _, attribute = path.partition(":")
    if not attribute:
        raise typer.BadParameter(f"expected module:attribute, got {path!r}")
    return getattr(importlib.import_module(module), attribute)


def sink(spec: str) -> ResultSink:
    """``jsonl:DIR``, ``postgres:URL`` or ``s3://BUCKET/PREFIX``."""
    from kairos_evals.sinks import JsonlSink, PostgresSink, S3ParquetSink

    if spec.startswith("jsonl:"):
        return JsonlSink(Path(spec.removeprefix("jsonl:")))
    if spec.startswith("postgres:"):
        from sqlalchemy.ext.asyncio import create_async_engine

        return PostgresSink(create_async_engine(spec.removeprefix("postgres:")))
    if spec.startswith("s3://"):
        bucket, _, prefix = spec.removeprefix("s3://").partition("/")
        return S3ParquetSink(bucket, prefix or "evals")
    raise typer.BadParameter(f"unknown sink {spec!r}")


def git_sha() -> str | None:
    try:
        out = subprocess.run(  # fixed argv, no shell
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def table(run: EvalRun) -> str:
    lines = [
        f"{run.suite}  {run.harness} {run.harness_version}  {run.model}  ({run.examples} examples)"
    ]
    for layer in ("outcome", "component", "model"):
        for s in (s for s in run.summary if s.layer == layer):
            spread = f"  p50 {s.p50:.4g}  p95 {s.p95:.4g}" if s.p50 is not None else ""
            lines.append(
                f"  {layer:<9} {s.metric_id:<28} n={s.n:<3} mean {s.mean:.3f}  "
                f"pass {s.pass_rate:.0%}{spread}"
            )
    return "\n".join(lines)


async def _run(
    suites: Sequence[str],
    harness: str,
    live: bool,
    outs: Sequence[str],
    gate: Path | None,
    update_baseline: Path | None,
) -> bool:
    adapter: HarnessAdapter = load(harness)(live=live)
    sinks = [sink(spec) for spec in outs]
    baseline = Baseline.load(gate) if gate else None
    updated = Baseline.load(update_baseline) if update_baseline else None
    sha = git_sha()
    ok = True
    for name in suites:
        suite: Suite = load(name)(live=live)
        run, results = await run_suite(suite, adapter, git_sha=sha)
        if baseline is not None:
            report = check(suite.name, run.summary, baseline)
            run = run.model_copy(
                update={"passed": report.passed, "gate": json.loads(report.model_dump_json())}
            )
            ok = ok and report.passed
        if updated is not None:
            updated = updated.with_suite(suite.name, run.summary)
        for target in sinks:
            await target.write(run, results)
        typer.echo(table(run))
        if run.gate:
            typer.echo(json.dumps(run.gate, indent=2))
    if updated is not None and update_baseline is not None:
        updated.save(update_baseline)
        typer.echo(f"baseline written to {update_baseline}")
    return ok


@app.command()
def run(
    suites: Annotated[list[str], typer.Argument(help="Suite factories, module:attribute.")],
    harness: Annotated[str, typer.Option(help="Harness adapter factory, module:attribute.")],
    live: Annotated[bool, typer.Option(help="Call real models (costs money).")] = False,
    out: Annotated[list[str] | None, typer.Option(help="jsonl:DIR, postgres:URL, s3://B/P")] = None,
    gate: Annotated[
        Path | None, typer.Option(help="Fail on regression against this baseline.")
    ] = None,
    update_baseline: Annotated[
        Path | None, typer.Option(help="Write these results as the new baseline.")
    ] = None,
) -> None:
    """Evaluate suites and write the results; exit 1 if the gate fails."""
    ok = asyncio.run(_run(suites, harness, live, out or [], gate, update_baseline))
    if not ok:
        typer.echo("regression gate FAILED", err=True)
        sys.exit(1)


@app.command()
def report(
    results: Annotated[Path, typer.Argument(help="A runs.jsonl written by jsonl:DIR.")],
) -> None:
    """Print the summaries of recorded runs, newest last."""
    for line in results.read_text().splitlines():
        if line.strip():
            typer.echo(table(EvalRun.model_validate_json(line)))


def main() -> None:
    app()
