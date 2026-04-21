from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import Settings, get_settings


def test_required_greffer_id_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GREFFER_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_loads_greffer_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "abc-123")
    s = Settings()
    assert s.greffer_id == "abc-123"


def test_defaults_apply_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    s = Settings()
    assert s.greffon_base_server == "https://api.greffon.io"
    assert s.greffer_protocol == "https"
    assert s.greffer_ssl_verify is True
    assert s.greffer_address is None
    assert s.greffer_port == 8000
    assert s.greffer_public_host == "host.docker.internal"
    assert s.greffer_public_scheme == "https"
    assert s.greffon_path == Path("/data")
    assert s.docker_nginx_name == "greffer-nginx-1"
    assert s.crl_sync_interval == 300
    assert s.monitor_interval == 5
    assert s.skip_ops_migrations is False
    assert s.logger_name == "greffer"


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFON_PATH", "/tmp/alt")
    monkeypatch.setenv("CRL_SYNC_INTERVAL", "60")
    monkeypatch.setenv("GREFFER_SSL_VERIFY", "false")
    s = Settings()
    assert s.greffon_path == Path("/tmp/alt")
    assert s.crl_sync_interval == 60
    assert s.greffer_ssl_verify is False


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    a = get_settings()
    b = get_settings()
    assert a is b


def test_protocol_literal_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_PROTOCOL", "ftp")
    with pytest.raises(ValidationError):
        Settings()
