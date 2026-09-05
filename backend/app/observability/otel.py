"""Optional OpenTelemetry tracing. Activated only when OTEL_EXPORTER_OTLP_ENDPOINT is set and the
instrumentation packages are installed (see requirements-observability.txt)."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def setup_tracing(app, engine) -> bool:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning(f"[OTEL] endpoint configured but instrumentation not installed ({exc}); pip install -r requirements-observability.txt")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "revenueshield-api")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)
    logger.info(f"[OTEL] tracing enabled -> {endpoint}")
    return True
