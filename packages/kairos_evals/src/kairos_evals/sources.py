"""Trace sources."""

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from kairos_evals.protocols import TraceFilter
from kairos_evals.trace import Span, Trace, from_unix_nano

_STATUS_ERROR = "STATUS_CODE_ERROR"


def _value(any_value: dict[str, Any]) -> JsonValue:
    """Decode an OTLP ``AnyValue``."""
    if "stringValue" in any_value:
        return str(any_value["stringValue"])
    if "intValue" in any_value:
        return int(any_value["intValue"])  # int64 is a string in OTLP/JSON
    if "doubleValue" in any_value:
        return float(any_value["doubleValue"])
    if "boolValue" in any_value:
        return bool(any_value["boolValue"])
    if "arrayValue" in any_value:
        return [_value(item) for item in any_value["arrayValue"].get("values", [])]
    return None


def _span(raw: dict[str, Any]) -> Span:
    status = raw.get("status", {}).get("code")
    return Span(
        trace_id=raw["traceId"],
        span_id=raw["spanId"],
        parent_span_id=raw.get("parentSpanId") or None,
        name=raw["name"],
        start=from_unix_nano(raw["startTimeUnixNano"]),
        end=from_unix_nano(raw["endTimeUnixNano"]),
        attributes={a["key"]: _value(a["value"]) for a in raw.get("attributes", [])},
        is_error=status in (_STATUS_ERROR, 2),
    )


def traces_from_otlp(documents: Iterable[dict[str, Any]]) -> list[Trace]:
    """Group the spans of OTLP/JSON export documents into traces."""
    spans: dict[str, list[Span]] = {}
    for document in documents:
        for resource_spans in document.get("resourceSpans", []):
            for scope_spans in resource_spans.get("scopeSpans", []):
                for raw in scope_spans.get("spans", []):
                    span = _span(raw)
                    spans.setdefault(span.trace_id, []).append(span)
    return [
        Trace(trace_id=trace_id, spans=tuple(sorted(group, key=lambda s: s.start)))
        for trace_id, group in spans.items()
    ]


class OtlpJsonTraceSource:
    """Reads OTLP/JSON trace exports: a file holding one JSON document per line.

    This is the format the OpenTelemetry Collector's file exporter writes, so any
    instrumented agent can produce input for the evaluator.
    """

    def __init__(self, *paths: Path) -> None:
        self._paths = paths

    async def load(self, filter: TraceFilter | None = None) -> AsyncIterator[Trace]:
        documents = (
            json.loads(line)
            for path in self._paths
            for line in path.read_text().splitlines()
            if line.strip()
        )
        for trace in traces_from_otlp(documents):
            if filter is None or _matches(trace, filter):
                yield trace


def _matches(trace: Trace, filter: TraceFilter) -> bool:
    if filter.trace_ids is not None and trace.trace_id not in filter.trace_ids:
        return False
    root = trace.root
    attributes = root.attributes if root else {}
    return all(attributes.get(key) == value for key, value in filter.attribute_equals.items())
