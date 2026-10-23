"""Structured JSON logging with a correlation ID (D5).

The correlation ID is the run ID once a run exists, or the request ID before
that. It travels in a contextvar inside a process, in the `X-Correlation-ID`
header between services, and in the job payload across the queue.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
agent_name: contextvars.ContextVar[str] = contextvars.ContextVar("agent_name", default="-")

CORRELATION_HEADER = "X-Correlation-ID"


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "correlation_id": correlation_id.get(),
            "agent": agent_name.get(),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:
                payload["trace_id"] = format(ctx.trace_id, "032x")
        except Exception:  # tracing optional
            pass
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(service: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Log with structured fields: log(logger, logging.INFO, "tool call", tool="search")."""
    logger.log(level, msg, extra={"fields": fields})
