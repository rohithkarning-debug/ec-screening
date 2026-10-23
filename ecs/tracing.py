"""OpenTelemetry tracing exported to Jaeger over OTLP (D5).

Tracing is switched on when OTEL_EXPORTER_OTLP_ENDPOINT is set. Trace context
is carried across HTTP calls in W3C `traceparent` headers and across the Redis
queue inside the job payload, so one run is a single trace in Jaeger.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterator

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from ecs.config import settings

_configured = False


def setup_tracing(service: str) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    endpoint = settings.otel_endpoint
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer(name: str = "ecs") -> trace.Tracer:
    return trace.get_tracer(name)


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield s


def inject_headers(headers: dict | None = None) -> dict:
    headers = dict(headers or {})
    propagate.inject(headers)
    return headers


def extract_context(carrier: dict):
    return propagate.extract(carrier or {})
