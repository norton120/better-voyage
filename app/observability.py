"""OpenTelemetry bootstrap + helpers used across the app.

Call `setup_observability(app, engine)` at FastAPI startup. Exporter
defaults to `console` so development works with no infrastructure;
set `BV_OTEL_EXPORTER=otlp` to ship to a collector (see the
`observability` profile in `compose.yaml`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app import __version__
from app.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

_configured = False


def setup_observability(
    app: FastAPI | None = None,
    engine: AsyncEngine | None = None,
) -> None:
    """Initialize tracing, metrics, and auto-instrumentation.

    Idempotent — safe to call repeatedly (e.g., in test fixtures).
    """
    global _configured
    if _configured:
        return

    settings = get_settings()

    if settings.otel_exporter == "none":
        _configured = True
        return

    resource = Resource.create(
        {
            SERVICE_NAME: settings.otel_service_name,
            SERVICE_VERSION: __version__,
            "deployment.environment": settings.env,
        }
    )

    sampler = ParentBased(TraceIdRatioBased(settings.otel_sample_ratio))
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    trace.set_tracer_provider(tracer_provider)

    if settings.otel_exporter == "console":
        span_exporter: Any = ConsoleSpanExporter()
        metric_exporter: Any = ConsoleMetricExporter()
    else:  # "otlp"
        span_exporter = OTLPSpanExporter(
            endpoint=f"{settings.otel_endpoint}/v1/traces"
        )
        metric_exporter = OTLPMetricExporter(
            endpoint=f"{settings.otel_endpoint}/v1/metrics"
        )

    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=15_000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    LoggingInstrumentor().instrument(set_logging_format=False)
    HTTPXClientInstrumentor().instrument()
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    _configured = True


def tracer(name: str = "better-voyage") -> trace.Tracer:
    return trace.get_tracer(name)


def meter(name: str = "better-voyage") -> metrics.Meter:
    return metrics.get_meter(name)
