"""Framework-agnostic agent eval library.

Consumes OpenTelemetry traces. Must not import any other ``kairos_*`` package (ADR-004).
"""

from kairos_evals.evaluator import Evaluator, load_examples, summarize
from kairos_evals.protocols import Metric, ResultSink, TraceFilter, TraceSource
from kairos_evals.sinks import JsonlSink
from kairos_evals.sources import OtlpJsonTraceSource, traces_from_otlp
from kairos_evals.trace import Span, Trace
from kairos_evals.types import EvalResult, Example, Score

__all__ = [
    "EvalResult",
    "Evaluator",
    "Example",
    "JsonlSink",
    "Metric",
    "OtlpJsonTraceSource",
    "ResultSink",
    "Score",
    "Span",
    "Trace",
    "TraceFilter",
    "TraceSource",
    "load_examples",
    "summarize",
    "traces_from_otlp",
]
