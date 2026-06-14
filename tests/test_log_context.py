"""Tests for the log-context filter + worker derivation (Feature #4)."""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.log_context import (
    ContextFilter,
    _current_worker,
    instance_id_var,
    request_id_var,
)


def test_context_filter_sets_fields_from_vars():
    t1 = request_id_var.set("r1")
    t2 = instance_id_var.set("i1")
    try:
        rec = logging.makeLogRecord({})
        assert ContextFilter().filter(rec) is True
        assert rec.request_id == "r1"
        assert rec.instance_id == "i1"
        # No asyncio task in this sync context -> worker is None.
        assert rec.worker is None
    finally:
        request_id_var.reset(t1)
        instance_id_var.reset(t2)


def test_context_filter_defaults_none_when_unset():
    rec = logging.makeLogRecord({})
    ContextFilter().filter(rec)
    assert rec.request_id is None
    assert rec.instance_id is None
    assert rec.worker is None


@pytest.mark.asyncio
async def test_worker_derived_from_greffer_task_name():
    asyncio.current_task().set_name("greffer-monitor")
    assert _current_worker() == "greffer-monitor"


@pytest.mark.asyncio
async def test_worker_none_for_non_greffer_task_name():
    asyncio.current_task().set_name("Task-123")
    assert _current_worker() is None
