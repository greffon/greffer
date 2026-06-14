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


def test_malformed_log_max_file_falls_back_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A typo in this optional knob must NOT crash startup (codex P2 on #72): a
    # ValidationError here would take down every instance operation.
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_INSTANCE_LOG_MAX_FILE", "2x")
    s = Settings()
    assert s.greffer_instance_log_max_file == 3


def test_valid_log_max_file_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_INSTANCE_LOG_MAX_FILE", "5")
    s = Settings()
    assert s.greffer_instance_log_max_file == 5


def test_log_format_defaults_json_and_coerces_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    assert Settings().greffer_log_format == "json"  # default
    monkeypatch.setenv("GREFFER_LOG_FORMAT", "yaml")  # bad -> default, no crash
    assert Settings().greffer_log_format == "json"
    monkeypatch.setenv("GREFFER_LOG_FORMAT", "text")
    assert Settings().greffer_log_format == "text"


def test_log_level_coerces_and_uppercases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_LOG_LEVEL", "debug")
    assert Settings().greffer_log_level == "DEBUG"
    monkeypatch.setenv("GREFFER_LOG_LEVEL", "loud")  # invalid -> INFO, no crash
    assert Settings().greffer_log_level == "INFO"


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


def test_heartbeat_interval_defaults_to_5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.delenv("HEARTBEAT_INTERVAL", raising=False)
    get_settings.cache_clear()
    assert get_settings().heartbeat_interval == 5


def test_heartbeat_interval_binds_unprefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.setenv("HEARTBEAT_INTERVAL", "9")
    get_settings.cache_clear()
    assert get_settings().heartbeat_interval == 9


def test_heartbeat_interval_rejects_non_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.setenv("HEARTBEAT_INTERVAL", "0")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_greffer_version_truncated_to_32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.setenv("GREFFER_VERSION", "0.3.3-rc1-42-gdeadbeef-dirty-20260611-extra")
    get_settings.cache_clear()
    v = get_settings().greffer_version
    assert len(v) == 32
    assert v == "0.3.3-rc1-42-gdeadbeef-dirty-2026"[:32]
