"""Record the M5 suites live: real Claude, real web, real judge and simulator calls.

Costs money and reaches the network. Run with ``just record-evals [suite ...]``,
which loads keys from ``.env`` and sets ``LIVE=1``. A spend cap stops the run before
it goes over (``KAIROS_EVALS_MAX_USD``, default 2.50). Re-recording a case replaces
its cassettes; judge and simulator answers are appended by request hash.

    just record-evals scheduling                 # one suite
    just record-evals intent_tools --only laundry_now,cancel_that_thing
    just record-evals all --haiku                # the layer-3 model comparison
    just record-evals intent_tools --judge-only  # new judge prompts, agent replayed
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from kairos_evals import EvalResult, Example, ModelCassette, Suite, run_suite
from kairos_evals.judge import JudgeMetric
from kairos_evals.metrics import ClarificationDetector
from tests.evals import suites
from tests.evals.kairos_adapter import KairosAdapter, haiku


class OverBudget(RuntimeError):
    pass


def cassettes(suite: Suite) -> list[ModelCassette]:
    found: list[ModelCassette] = []
    for metric in suite.metrics:
        if isinstance(metric, JudgeMetric) and isinstance(metric.judge.model, ModelCassette):
            found.append(metric.judge.model)
        detector = getattr(metric, "detector", None)
        if isinstance(detector, ClarificationDetector) and isinstance(
            detector.model, ModelCassette
        ):
            found.append(detector.model)
    conversation = suite.conversation_judge.model if suite.conversation_judge else None
    for model in (suite.simulator, conversation):
        if isinstance(model, ModelCassette):
            found.append(model)
    return list({id(m): m for m in found}.values())


async def record(
    names: Sequence[str], only: set[str], use_haiku: bool, cap: float, *, judge_only: bool = False
) -> None:
    """``judge_only`` replays the agent from its cassettes and records only judge,
    detector and simulator answers that are missing (after a judge prompt change)."""
    agent_live = not judge_only
    adapter: KairosAdapter = haiku(live=agent_live) if use_haiku else KairosAdapter(live=agent_live)
    factories = {f.__name__: f for f in (*suites.ALL, *suites.PENDING, *suites.COMPARISON)}
    models: list[ModelCassette] = []  # every judge/simulator used so far, for the spend total

    def spent() -> float:
        # A replayed agent reports its recorded cost, which costs nothing now.
        agent = adapter.cost_usd if agent_live else 0.0
        return agent + sum(m.cost_usd for m in models)

    def progress(example: Example, results: list[EvalResult]) -> None:
        failed = [r.metric_id for r in results if r.score.applicable and not r.score.passed]
        print(
            f"  {example.id}: {'ok' if not failed else 'FAILED ' + ', '.join(failed)}"
            f"  (total ${spent():.3f})",
            flush=True,
        )
        if spent() > cap:
            raise OverBudget(f"spend ${spent():.3f} passed the cap ${cap:.2f}")

    for name in names:
        suite = factories[name](live=True)
        if use_haiku:  # the comparison measures the agent model only: no judge calls
            suite = Suite(
                name=suite.name,
                examples=suite.examples,
                metrics=[m for m in suite.metrics if not isinstance(m, JudgeMetric)],
                simulator=suite.simulator,
            )
        if only:
            suite = Suite(
                name=suite.name,
                examples=[e for e in suite.examples if e.id in only],
                metrics=suite.metrics,
                simulator=suite.simulator,
                conversation_judge=suite.conversation_judge,
            )
        models += cassettes(suite)
        print(f"{suite.name}: {len(suite.examples)} cases on {adapter.model}", flush=True)
        await run_suite(suite, adapter, on_example=progress)
    print(f"done: spent ${spent():.3f} (agent {'live' if agent_live else 'replayed'})")


def main() -> None:
    if os.environ.get("LIVE") != "1":
        sys.exit("refusing to spend money without LIVE=1 (use `just record-evals`)")
    parser = argparse.ArgumentParser()
    parser.add_argument("suites", nargs="*", default=["all"])
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--haiku", action="store_true", help="record the Haiku comparison")
    parser.add_argument(
        "--judge-only", action="store_true", help="replay the agent; record missing judge calls"
    )
    args = parser.parse_args()
    default = suites.COMPARISON if args.haiku else suites.ALL
    names = [f.__name__ for f in default] if args.suites in ([], ["all"]) else args.suites
    cap = float(os.environ.get("KAIROS_EVALS_MAX_USD", "2.50"))
    only = {i for i in args.only.split(",") if i}
    try:
        asyncio.run(record(names, only, args.haiku, cap, judge_only=args.judge_only))
    except OverBudget as error:
        sys.exit(f"stopped: {error}")


if __name__ == "__main__":
    main()
