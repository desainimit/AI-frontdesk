import json
import logging
import logging.config
from contextvars import ContextVar
from datetime import UTC, datetime

from opentelemetry import trace

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Only emit known event metadata; never serialize exception messages or arbitrary extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # Third-party messages may contain URLs, SQL parameters, or user content.
            "event": record.getMessage() if record.name.startswith("app.") else "library_event",
        }
        if request_id := request_id_var.get():
            payload["request_id"] = request_id
        for key in (
            "method",
            "status_code",
            "duration_ms",
            "dependency",
            "error_type",
            "provider",
            "reason",
            "tenant_id",
            "call_id",
            "event_id",
            "outcome",
        ):
            value: object = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["trace_id"] = format(context.trace_id, "032x")
            payload["span_id"] = format(context.span_id, "016x")
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json",
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
            "loggers": {
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": False},
                "sqlalchemy.engine": {"level": "WARNING"},
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
            },
        }
    )
