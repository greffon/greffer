from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.config

from app.settings import Settings

# Record attributes that are NOT caller-supplied ``extra={...}`` fields; used to
# pull only the extras into the JSON without dragging in the LogRecord
# internals. The context fields are emitted explicitly below.
_RESERVED = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "message", "asctime", "request_id", "instance_id", "worker", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line (greffer-observability epic, Feature #4).

    Emits ``timestamp/level/logger/message/greffer_id`` always, the context
    fields (``request_id``/``instance_id``/``worker``) when set by
    ``ContextFilter``, any caller ``extra={...}`` keys, and ``exc`` when an
    exception is logged. Hand-rolled (no new dependency — the greffer image is
    deliberately lean, the same reasoning the epic uses to defer ``/metrics``)."""

    def __init__(self, greffer_id: str = "") -> None:
        super().__init__()
        self.greffer_id = greffer_id

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # A %-arg-count mismatch at the call site would raise here; the
            # stdlib formatter tolerates it, so we do too (log the raw template
            # rather than let one bad call site break the formatter).
            message = str(record.msg)
        payload = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "greffer_id": self.greffer_id,
        }
        for attr in ("request_id", "instance_id", "worker"):
            value = getattr(record, attr, None)
            if value is not None:
                payload[attr] = value
        # Caller-supplied structured fields via logger.x(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    level = settings.greffer_log_level
    fmt = "json" if settings.greffer_log_format == "json" else "text"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "context": {"()": "app.log_context.ContextFilter"},
            },
            "formatters": {
                "text": {
                    "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                },
                "json": {
                    "()": "app.logging.JsonFormatter",
                    "greffer_id": settings.greffer_id,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": fmt,
                    "filters": ["context"],
                },
            },
            "loggers": {
                name: {
                    "handlers": ["console"],
                    "level": level,
                    "propagate": False,
                }
                for name in (
                    settings.logger_name, "uvicorn.error", "uvicorn.access")
            },
        }
    )
