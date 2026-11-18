"""Framework-agnostic agent eval library.

Consumes OpenTelemetry traces. Must not import any other ``kairos_*`` package (ADR-004).
"""

from kairos_evals.evaluator import Evaluator, load_examples, summarize
from kairos_evals.gate import Baseline, GatePolicy, GateReport, check
from kairos_evals.judge import Judge, JudgeMetric, judge_metrics
from kairos_evals.models import (
    AnthropicJsonModel,
    CassetteMiss,
    ChatMessage,
    JsonModel,
    ModelCassette,
    ModelRefusal,
)
from kairos_evals.protocols import Metric, ResultSink, TraceFilter, TraceSource
from kairos_evals.runner import HarnessAdapter, Suite, run_suite
from kairos_evals.simulator import (
    PERSONAS,
    Conversation,
    ConversationJudge,
    ConversationSession,
    UserSimulator,
    simulate,
)
from kairos_evals.sinks import JsonlSink, PostgresSink, S3ParquetSink
from kairos_evals.sources import OtlpJsonTraceSource, traces_from_otlp
from kairos_evals.stats import cohens_kappa, percentile, summarize_results
from kairos_evals.trace import Span, ToolRequest, Trace
from kairos_evals.types import (
    EvalResult,
    EvalRun,
    Example,
    ExpectedSchedule,
    MetricSummary,
    Score,
    Simulation,
    not_applicable,
)

__all__ = [
    "PERSONAS",
    "AnthropicJsonModel",
    "Baseline",
    "CassetteMiss",
    "ChatMessage",
    "Conversation",
    "ConversationJudge",
    "ConversationSession",
    "EvalResult",
    "EvalRun",
    "Evaluator",
    "Example",
    "ExpectedSchedule",
    "GatePolicy",
    "GateReport",
    "HarnessAdapter",
    "JsonModel",
    "JsonlSink",
    "Judge",
    "JudgeMetric",
    "Metric",
    "MetricSummary",
    "ModelCassette",
    "ModelRefusal",
    "OtlpJsonTraceSource",
    "PostgresSink",
    "ResultSink",
    "S3ParquetSink",
    "Score",
    "Simulation",
    "Span",
    "Suite",
    "ToolRequest",
    "Trace",
    "TraceFilter",
    "TraceSource",
    "UserSimulator",
    "check",
    "cohens_kappa",
    "judge_metrics",
    "load_examples",
    "not_applicable",
    "percentile",
    "run_suite",
    "simulate",
    "summarize",
    "summarize_results",
    "traces_from_otlp",
]
