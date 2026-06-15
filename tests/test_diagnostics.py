"""Tests for the diagnostic-counter helper (Feature #4 fast-follow)."""
from __future__ import annotations

import json
import logging

import pytest

from app.diagnostics import diag
from app.logging import JsonFormatter


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def cap():
    lg = logging.getLogger("greffer")
    handler = _Capture()
    old_level = lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)


def test_diag_emits_event_as_message_and_fields(cap):
    diag("compose_op", op="start", outcome="ok", duration_ms=12)
    rec = cap.records[-1]
    assert rec.getMessage() == "compose_op"
    assert rec.event == "compose_op"  # also a field, for field-based filtering
    assert rec.op == "start"
    assert rec.outcome == "ok"
    assert rec.duration_ms == 12
    assert rec.levelno == logging.INFO


def test_diag_respects_explicit_level(cap):
    diag("monitor_tick", level=logging.DEBUG, duration_ms=3, instances=2)
    rec = cap.records[-1]
    assert rec.levelno == logging.DEBUG
    assert rec.instances == 2


def test_diag_fields_serialize_to_json():
    # End-to-end with the Feature #4 formatter: the diag fields land as JSON keys.
    rec = logging.LogRecord(
        "greffer", logging.WARNING, "p", 1, "status_callback", None, None)
    rec.event = "status_callback"
    rec.outcome = "rejected"
    rec.status_code = 403
    out = json.loads(JsonFormatter(greffer_id="g").format(rec))
    assert out["message"] == "status_callback"
    assert out["event"] == "status_callback"
    assert out["outcome"] == "rejected"
    assert out["status_code"] == 403
