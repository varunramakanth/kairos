"""Tracer provider construction and exporter helpers.

Nothing here touches global OpenTelemetry state: callers get a ``TracerProvider``
and pass a tracer to ``TracingHooks``. That keeps tests isolated from each other.
"""

import base64
from collections.abc import Mapping, Sequence

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = "kairos"


def langfuse_headers(public_key: str, secret_key: str) -> dict[str, str]:
    """Headers for Langfuse's OTLP endpoint (``<host>/api/public/otel``).

    Langfuse accepts OTLP over HTTP only (no gRPC), with Basic auth made from the
    project keys. The ingestion-version header makes traces show up promptly.
    """
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"}


def configure_tracing(
    *,
    service_version: str,
    otlp_endpoint: str | None = None,
    otlp_headers: Mapping[str, str] | None = None,
    processors: Sequence[SpanProcessor] = (),
) -> TracerProvider:
    """Build a tracer provider.

    Args:
        otlp_endpoint: Base OTLP/HTTP endpoint without the ``/v1/traces`` suffix,
            e.g. ``http://localhost:3100/api/public/otel``. ``None`` disables export.
        processors: Extra span processors (file export, in-memory capture for tests).
    """
    resource = Resource.create({"service.name": SERVICE_NAME, "service.version": service_version})
    provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces", headers=dict(otlp_headers or {})
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    for processor in processors:
        provider.add_span_processor(processor)
    return provider
