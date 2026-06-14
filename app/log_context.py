"""Per-request / per-task log context (greffer-observability epic, Feature #4).

A ``contextvars``-based context that the JSON formatter surfaces as log fields,
so a manager-side action correlates with the greffer-side work it triggered:

- ``request_id``: set by the request-ID middleware for the duration of a request
  (propagated from an inbound ``X-Request-ID`` or freshly generated).
- ``instance_id``: set around a specific greffon's compose operation.
- ``worker``: derived from the running asyncio task's name (start_workers names
  them ``greffer-monitor`` etc.), so background-task logs are attributable
  without threading a var through every worker.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None)
instance_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "instance_id", default=None)


def _current_worker() -> str | None:
    """The greffer background-task name, if this log call is inside one."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None  # no running loop (sync context)
    if task is None:
        return None
    name = task.get_name()
    return name if name and name.startswith("greffer-") else None


class ContextFilter(logging.Filter):
    """Attach the context fields to every record so the formatter can emit them.
    Always returns True (a filter used purely to enrich, never to drop)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.instance_id = instance_id_var.get()
        record.worker = _current_worker()
        return True
