"""Running a suite against a harness: define inputs, evaluate, share results.

A ``HarnessAdapter`` turns an example into a trace. The Kairos harness is one
adapter; any agent that emits OpenTelemetry GenAI spans can be another. Adapters
live outside this package, so it never imports the agent it evaluates.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from kairos_evals.evaluator import Evaluator
from kairos_evals.models import JsonModel
from kairos_evals.protocols import Metric, ResultSink
from kairos_evals.simulator import (
    ConversationJudge,
    ConversationSession,
    UserSimulator,
    simulate,
)
from kairos_evals.stats import summarize_results
from kairos_evals.trace import Trace
from kairos_evals.types import EvalResult, EvalRun, Example


class HarnessAdapter(Protocol):
    """Runs the agent under test on one example and returns the run's trace."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def run(self, example: Example) -> Trace: ...

    async def conversation(self, example: Example) -> ConversationSession:
        """A session for a simulated multi-turn case (``example.simulation``)."""
        ...


@dataclass(frozen=True)
class Suite:
    """A named dataset with the metrics that score it.

    Simulated examples also need ``simulator`` (the model playing the user) and are
    scored per turn by ``metrics`` and per conversation by ``conversation_judge``.
    """

    name: str
    examples: Sequence[Example]
    metrics: Sequence[Metric]
    simulator: JsonModel | None = None
    conversation_judge: ConversationJudge | None = None
    description: str = ""
    tags: frozenset[str] = field(default_factory=frozenset[str])


async def run_suite(
    suite: Suite,
    adapter: HarnessAdapter,
    *,
    sinks: Sequence[ResultSink] = (),
    git_sha: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_example: Callable[[Example, list[EvalResult]], None] | None = None,
) -> tuple[EvalRun, list[EvalResult]]:
    """Evaluate every example in order and write the run to every sink.

    Examples run one at a time: replay must be deterministic and live recording
    should not burst the provider.
    """
    started = now()
    evaluator = Evaluator(suite.metrics)
    results: list[EvalResult] = []
    for example in suite.examples:
        example_results: list[EvalResult] = []
        if example.simulation is not None:
            if suite.simulator is None:
                raise ValueError(f"suite {suite.name} has simulated examples but no simulator")
            session = await adapter.conversation(example)
            conversation = await simulate(
                session, UserSimulator(suite.simulator, example.simulation), example
            )
            turn_cases = [(turn.trace, example) for turn in conversation.turns]
            example_results += await evaluator.evaluate(turn_cases)
            if suite.conversation_judge is not None:
                example_results += await suite.conversation_judge.evaluate(conversation, example)
        else:
            trace = await adapter.run(example)
            example_results += await evaluator.evaluate([(trace, example)])
        if on_example is not None:
            on_example(example, example_results)
        results += example_results
    run = EvalRun(
        id=f"evr_{uuid.uuid4().hex[:16]}",
        suite=suite.name,
        harness=adapter.name,
        harness_version=adapter.version,
        model=adapter.model,
        started_at=started,
        finished_at=now(),
        git_sha=git_sha,
        examples=len(suite.examples),
        summary=summarize_results(results),
    )
    for sink in sinks:
        await sink.write(run, results)
    return run, results
