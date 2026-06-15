"""Diagnostic counters as structured log fields (greffer-observability epic,
Feature #4 fast-follow).

Each countable occurrence (a compose op by outcome, a monitor tick's duration, a
heartbeat / status-callback failure, a registration-state change) is emitted as
ONE structured log line, so the numbers are greppable / aggregatable straight
from the JSON logs with no scrape endpoint and no new dependency. ``event`` is
both the log message AND a field (so a log pipeline can filter on the field),
and the keyword fields ride the line via Feature #4's ``extra`` -> JSON
mechanism; ``request_id`` / ``instance_id`` / ``worker`` come from the context
filter automatically.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("greffer")


def diag(event: str, *, level: int = logging.INFO, **fields) -> None:
    """Emit a structured diagnostic event. ``fields`` must use names that are not
    LogRecord-internal (op/outcome/duration_ms/status_code/...); the formatter
    already protects its own output keys."""
    logger.log(level, event, extra={"event": event, **fields})
