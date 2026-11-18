"""Spans as OTLP/JSON, the interchange format ``kairos_evals`` reads."""

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def spans_to_otlp_json(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    """Encode spans as an OTLP/JSON ``ExportTraceServiceRequest``.

    Protobuf's JSON mapping writes byte fields as base64, but the OTLP/JSON spec
    requires trace and span IDs in hex, so those fields are converted.
    """
    document = MessageToDict(encode_spans(spans))
    for resource_spans in document.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                for name in _ID_FIELDS:
                    if name in span:
                        span[name] = base64.b64decode(span[name]).hex()
    return document


class OtlpJsonFileExporter(SpanExporter):
    """Appends each exported batch to a file as one OTLP/JSON document per line."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._path.open("a") as file:
            file.write(json.dumps(spans_to_otlp_json(spans)) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True
