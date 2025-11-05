# core/telemetry/otel.py
from __future__ import annotations
import os, json, logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

# Try Azure's OTel log handler; if missing, use Opencensus AzureLogHandler
try:
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter, AzureMonitorLogHandler  # type: ignore
    _USE_OPENCENSUS = False
except Exception:
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter  # type: ignore
    from opencensus.ext.azure.log_exporter import AzureLogHandler as AzureMonitorLogHandler  # type: ignore
    _USE_OPENCENSUS = True

from core.redact import scrub

SERVICE_NAME = "teams-ai-servicedesk"
logger = logging.getLogger("app")

def setup_tracing(app) -> None:
    conn = os.getenv("APPINSIGHTS_CONNECTION_STRING")
    if not conn:
        return

    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(AzureMonitorTraceExporter.from_connection_string(conn))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

def setup_log_export() -> None:
    conn = os.getenv("APPINSIGHTS_CONNECTION_STRING")
    if not conn:
        return

    handler = AzureMonitorLogHandler(connection_string=conn)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)

    which = "Opencensus" if _USE_OPENCENSUS else "OTel"
    logging.getLogger("app").info(json.dumps({"event": "log_exporter_attached", "via": which}))

def safe_log(event: dict) -> None:
    if os.getenv("FEATURE_PII_REDACT", "true").lower() == "true":
        event = scrub(event)
    try:
        logger.info(json.dumps(event, ensure_ascii=False))
    except Exception:
        logger.info("event=%s", event)
