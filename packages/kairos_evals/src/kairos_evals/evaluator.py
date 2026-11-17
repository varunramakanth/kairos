"""Runs a set of metrics over traces and summarizes the results."""

from collections.abc import Iterable, Sequence
from pathlib import Path

from kairos_evals.protocols import Metric
from kairos_evals.trace import Trace
from kairos_evals.types import EvalResult, Example


class Evaluator:
    def __init__(self, metrics: Sequence[Metric]) -> None:
        if len({m.id for m in metrics}) != len(metrics):
            raise ValueError("metric ids must be unique")
        self._metrics = tuple(metrics)

    async def evaluate(self, cases: Iterable[tuple[Trace, Example | None]]) -> list[EvalResult]:
        """Score every ``(trace, example)`` pair with every metric."""
        results: list[EvalResult] = []
        for trace, example in cases:
            for metric in self._metrics:
                results.append(
                    EvalResult(
                        metric_id=metric.id,
                        layer=metric.layer,
                        kind=metric.kind,
                        trace_id=trace.trace_id,
                        example_id=example.id if example else None,
                        score=await metric.evaluate(trace, example),
                    )
                )
        return results


def summarize(results: Sequence[EvalResult]) -> dict[str, float]:
    """Mean score per metric."""
    values: dict[str, list[float]] = {}
    for result in results:
        values.setdefault(result.metric_id, []).append(result.score.value)
    return {metric_id: sum(v) / len(v) for metric_id, v in sorted(values.items())}


def load_examples(path: Path) -> list[Example]:
    """Read a golden dataset: one JSON example per line."""
    lines = path.read_text().splitlines()
    return [Example.model_validate_json(line) for line in lines if line.strip()]
