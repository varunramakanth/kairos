"""M5 suites replayed offline and held to the regression gate.

Every case was recorded live (``just record-evals``): the agent's model turns, its
tool results, the judge's verdicts and the simulated users' lines. Replaying them
is free and deterministic, so this runs on every PR. A metric that falls more than
the tolerance below ``baseline.json``, or under a hard floor (approval gates and
injection resistance at 1.0), fails the build.

After an intended change in behaviour, re-record the affected cases and refresh the
baseline with ``just evals-baseline``; the diff of ``baseline.json`` is the review.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from kairos_evals import Baseline, JsonlSink, Suite, check, run_suite
from kairos_evals.cli import table
from tests.evals import suites
from tests.evals.kairos_adapter import KairosAdapter

pytestmark = pytest.mark.evals

BASELINE = Path(__file__).parent / "baseline.json"


@pytest.mark.parametrize("factory", suites.ALL, ids=lambda f: f.__name__)
async def test_suite_replays_and_passes_the_gate(
    factory: Callable[..., Suite], tmp_path: Path
) -> None:
    suite = factory(live=False)
    run, results = await run_suite(suite, KairosAdapter(live=False), sinks=[JsonlSink(tmp_path)])
    print("\n" + table(run))

    assert len({r.example_id for r in results}) == len(suite.examples)
    baseline = Baseline.load(BASELINE)
    assert suite.name in baseline.suites, "no baseline for this suite: run `just evals-baseline`"
    report = check(suite.name, run.summary, baseline)
    assert report.passed, report.failures
