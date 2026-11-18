"""The metric library, in three layers: outcome, component, model.

Layer 3 (model) is not a metric class: it is the same suites run per model and
provider, compared on quality, cost and latency (see ``EvalRun.model``).
"""

from kairos_evals.metrics.budget import BudgetAdherence
from kairos_evals.metrics.citations import CitationGrounding
from kairos_evals.metrics.intent import (
    ClarificationDetector,
    ClarificationQuality,
    IntentAccuracy,
    infer_intent,
    resolve_intent,
)
from kairos_evals.metrics.outcome import CostPerTask, LatencyPerTask, TaskCompletion
from kairos_evals.metrics.planning import PlanCompletion
from kairos_evals.metrics.policy import ApprovalGateAdherence, InjectionResistance
from kairos_evals.metrics.scheduling import ScheduleCorrectness, cron_firings, same_schedule
from kairos_evals.metrics.tools import (
    RedundantCalls,
    ToolErrorRate,
    ToolParameterAccuracy,
    ToolSelectionAccuracy,
)

__all__ = [
    "ApprovalGateAdherence",
    "BudgetAdherence",
    "CitationGrounding",
    "ClarificationDetector",
    "ClarificationQuality",
    "CostPerTask",
    "InjectionResistance",
    "IntentAccuracy",
    "LatencyPerTask",
    "PlanCompletion",
    "RedundantCalls",
    "ScheduleCorrectness",
    "TaskCompletion",
    "ToolErrorRate",
    "ToolParameterAccuracy",
    "ToolSelectionAccuracy",
    "cron_firings",
    "infer_intent",
    "resolve_intent",
    "same_schedule",
]
