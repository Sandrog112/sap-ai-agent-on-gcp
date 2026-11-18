"""Traces and logs of both deployed services: spans go to Cloud Trace, and JSON lines on stdout become Cloud Logging
entries linked to the active trace, so a trace id finds everything one request did across the agent and the tools."""

import json
import logging
import os
import sys
from typing import Any

from opentelemetry import trace

_RECORD_FIELDS = {*logging.makeLogRecord({}).__dict__, "message", "asctime", "color_message"}


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, using the field names Cloud Logging reads for severity and trace correlation.
    Anything passed as `extra=` becomes a field, which the log-based metrics of infra/observability.py rely on."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            **{key: value for key, value in record.__dict__.items() if key not in _RECORD_FIELDS},
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["logging.googleapis.com/trace"] = (
                f"projects/{os.getenv('GCP_PROJECT_ID')}/traces/{span.trace_id:032x}"
            )
            entry["logging.googleapis.com/spanId"] = f"{span.span_id:016x}"
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_telemetry(service_name: str) -> None:
    """Set up logging for this process, and span export when TELEMETRY_ENABLED=true (set by deploy.py).

    Locally spans stay no-ops, so running a service needs no Google credentials for tracing.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for chatty in ("httpx", "mcp"):  # a line per HTTP and MCP request, on top of the one per tool call
        logging.getLogger(chatty).setLevel(logging.WARNING)

    if os.getenv("TELEMETRY_ENABLED") != "true" or isinstance(trace.get_tracer_provider(), _sdk_provider()):
        return

    # Imported here because the exporter pulls in the Cloud Trace client, which only a deployed service needs.
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(project_id=os.environ["GCP_PROJECT_ID"])))
    trace.set_tracer_provider(provider)
    # Adds the W3C traceparent header to every outgoing call, which joins the tool server's spans to the agent's trace.
    HTTPXClientInstrumentor().instrument()


def current_trace_id() -> str | None:
    """Id of the active trace, as Cloud Trace shows it."""
    span = trace.get_current_span().get_span_context()
    return f"{span.trace_id:032x}" if span.is_valid else None


def flush_telemetry() -> None:
    """Export the finished spans now. Serverless CPU is throttled once a response is sent, so a background export
    may otherwise not run until the next request."""
    flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if flush:
        flush()


def _sdk_provider() -> type:
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider
