"""Summaries over a suite's results, and judge-human agreement."""

import math
from collections.abc import Hashable, Sequence

from kairos_evals.types import EvalResult, MetricSummary


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..100) of a non-empty sequence."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize_results(results: Sequence[EvalResult]) -> list[MetricSummary]:
    """Per metric: applicable count, mean score, pass rate and measurement percentiles.

    Metrics whose every score was not applicable are reported with ``n = 0``.
    """
    groups: dict[str, list[EvalResult]] = {}
    for result in results:
        groups.setdefault(result.metric_id, []).append(result)
    summaries: list[MetricSummary] = []
    for metric_id, group in sorted(groups.items()):
        scored = [r.score for r in group if r.score.applicable]
        measured = [s.measurement for s in scored if s.measurement is not None]
        n = len(scored)
        summaries.append(
            MetricSummary(
                metric_id=metric_id,
                layer=group[0].layer,
                kind=group[0].kind,
                n=n,
                mean=sum(s.value for s in scored) / n if n else 1.0,
                pass_rate=sum(s.passed for s in scored) / n if n else 1.0,
                p50=percentile(measured, 50) if measured else None,
                p95=percentile(measured, 95) if measured else None,
            )
        )
    return summaries


def cohens_kappa[T: Hashable](a: Sequence[T], b: Sequence[T]) -> float:
    """Agreement between two raters beyond chance: 1 is perfect, 0 is chance level.

    Used to calibrate an LLM judge against human labels on the same items. A judge
    below about 0.6 should not gate anything.
    """
    if len(a) != len(b) or not a:
        raise ValueError("need two equally long, non-empty label sequences")
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    labels = set(a) | set(b)
    expected = sum((list(a).count(label) / n) * (list(b).count(label) / n) for label in labels)
    if expected == 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)
