"""Tests for instance compose log-rotation injection (Feature #3)."""
from __future__ import annotations

from apps.utils.docker.compose import _inject_instance_log_rotation


def test_injects_logging_into_each_service(monkeypatch):
    monkeypatch.setenv("GREFFER_INSTANCE_LOG_MAX_SIZE", "5m")
    monkeypatch.setenv("GREFFER_INSTANCE_LOG_MAX_FILE", "2")
    compose = {"services": {"web": {"image": "nginx"}, "db": {"image": "pg"}}}
    _inject_instance_log_rotation(compose)
    expected = {"driver": "json-file",
                "options": {"max-size": "5m", "max-file": "2"}}
    assert compose["services"]["web"]["logging"] == expected
    assert compose["services"]["db"]["logging"] == expected


def test_respects_catalog_authors_existing_logging(monkeypatch):
    compose = {"services": {"web": {"image": "nginx",
                                    "logging": {"driver": "syslog"}}}}
    _inject_instance_log_rotation(compose)
    # An explicit author choice is left untouched.
    assert compose["services"]["web"]["logging"] == {"driver": "syslog"}


def test_default_values_when_env_unset(monkeypatch):
    monkeypatch.delenv("GREFFER_INSTANCE_LOG_MAX_SIZE", raising=False)
    monkeypatch.delenv("GREFFER_INSTANCE_LOG_MAX_FILE", raising=False)
    compose = {"services": {"web": {"image": "nginx"}}}
    _inject_instance_log_rotation(compose)
    assert compose["services"]["web"]["logging"]["options"] == {
        "max-size": "10m", "max-file": "3"}


def test_no_services_is_safe():
    compose = {"version": "3"}
    _inject_instance_log_rotation(compose)  # must not raise
    assert "services" not in compose


def test_none_compose_is_safe():
    _inject_instance_log_rotation(None)  # must not raise
