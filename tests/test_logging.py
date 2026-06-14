"""Tests for the JSON log formatter + format selection (Feature #4)."""
from __future__ import annotations

import json
import logging
import sys

from app.logging import JsonFormatter, configure_logging


def _record(msg="hello", args=None, name="greffer", level=logging.INFO,
            exc_info=None):
    return logging.LogRecord(name, level, "p", 1, msg, args, exc_info)


def test_json_formatter_emits_core_fields():
    out = json.loads(JsonFormatter(greffer_id="g1").format(_record()))
    assert out["level"] == "INFO"
    assert out["logger"] == "greffer"
    assert out["message"] == "hello"
    assert out["greffer_id"] == "g1"
    assert "timestamp" in out
    # context fields are omitted when unset (no ContextFilter ran)
    assert "request_id" not in out and "worker" not in out


def test_json_formatter_interpolates_and_includes_context_and_extra():
    rec = _record("msg %s", ("x",))
    rec.request_id = "req1"
    rec.worker = "greffer-monitor"
    rec.instance_id = "inst-9"
    rec.custom_field = "v"  # caller extra={...}
    out = json.loads(JsonFormatter(greffer_id="g").format(rec))
    assert out["message"] == "msg x"
    assert out["request_id"] == "req1"
    assert out["worker"] == "greffer-monitor"
    assert out["instance_id"] == "inst-9"
    assert out["custom_field"] == "v"


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = _record("failed", level=logging.ERROR, exc_info=sys.exc_info())
    out = json.loads(JsonFormatter().format(rec))
    assert "exc" in out and "ValueError" in out["exc"]


def test_extra_cannot_overwrite_formatter_owned_fields():
    # A caller extra={...} must NOT shadow the authoritative fields (codex P2
    # on #73): no log line can spoof its own greffer_id / level / logger.
    rec = _record()
    rec.level = "SPOOFED"
    rec.logger = "evil"
    rec.greffer_id = "not-me"
    rec.timestamp = "1999"
    out = json.loads(JsonFormatter(greffer_id="g1").format(rec))
    assert out["level"] == "INFO"
    assert out["logger"] == "greffer"
    assert out["greffer_id"] == "g1"
    assert out["timestamp"] != "1999"


def test_json_formatter_tolerates_bad_arg_count():
    # A %-arg-count mismatch must not raise inside the formatter (the stdlib
    # tolerates it; the hand-rolled formatter must too).
    rec = _record("need %s %s", ("only-one",))
    out = json.loads(JsonFormatter().format(rec))
    assert out["message"] == "need %s %s"  # raw template, no crash


def test_configure_logging_selects_json(settings):
    settings.greffer_log_format = "json"  # type: ignore[misc]
    configure_logging(settings)
    fmt = logging.getLogger(settings.logger_name).handlers[0].formatter
    assert isinstance(fmt, JsonFormatter)


def test_configure_logging_selects_text(settings):
    settings.greffer_log_format = "text"  # type: ignore[misc]
    configure_logging(settings)
    fmt = logging.getLogger(settings.logger_name).handlers[0].formatter
    assert not isinstance(fmt, JsonFormatter)


def test_configure_logging_covers_apps_tree(settings):
    # apps.* loggers (the compose helper) must route through the JSON+context
    # handler, else the start/stop logs request_id correlates with would skip
    # it (codex P2 on #73).
    settings.greffer_log_format = "json"  # type: ignore[misc]
    configure_logging(settings)
    apps_logger = logging.getLogger("apps")
    assert apps_logger.handlers, "apps tree has no handler"
    assert any(isinstance(h.formatter, JsonFormatter)
               for h in apps_logger.handlers)
    # the context filter is attached so request_id/instance_id are surfaced
    assert any(f.__class__.__name__ == "ContextFilter"
               for h in apps_logger.handlers for f in h.filters)
