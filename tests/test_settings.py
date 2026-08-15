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
    # Security-relevant default (self-update v2): remote update is ON by default;
    # the cryptographic gates (digest-pinned updater image + cosign/floor) are the
    # real guard. Pin it here so a flip back to off is a deliberate, visible change.
    assert s.greffer_remote_update_enabled is True


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


def test_an_interval_that_outruns_the_window_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on 4f5bd5b: each value is in range on its own.

    A one-day window with a one-day interval leaves room for a single
    attempt, so one transient failure -- a deferred settle window, a capped
    tick, an unreachable manager -- and the certificate expires having never
    been retried. That is the outage this feature exists to prevent,
    reachable through configuration the validators accepted.
    """
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", "1")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_INTERVAL", "86400")
    with pytest.raises(ValidationError, match="renewal attempt"):
        Settings()


def test_a_one_day_window_is_fine_with_a_short_enough_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair is what is rejected, not the narrow window itself.

    3h leaves attempts at 0, 3, 9 and 21 hours -- four inside the day.
    """
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", "1")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_INTERVAL", "10800")
    s = Settings()
    assert s.greffer_cert_renewal_interval == 10800


def test_the_window_is_measured_against_the_backoff_not_even_spacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on 5479922: retries are exponential, not periodic.

    A one-day window with a 6h interval looks like four evenly spaced
    attempts and is not: after failures the delays are 6h, 12h then 24h, so
    the attempts land at 0, 6 and 18 hours and the fourth at 42 -- eighteen
    hours past the expiry it was supposed to beat.
    """
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", "1")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_INTERVAL", "21600")
    with pytest.raises(ValidationError, match="renewal attempt"):
        Settings()


def test_the_validator_and_the_worker_share_one_backoff_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two copies would drift and the validator would start lying."""
    from app.settings import CERT_RENEWAL_BACKOFF_CAP_SECONDS
    from app.workers import cert_renewal

    assert cert_renewal._BACKOFF_CAP_SECONDS is CERT_RENEWAL_BACKOFF_CAP_SECONDS


def test_the_shipped_defaults_leave_room_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validator that rejected the defaults would break every boot."""
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.delenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("GREFFER_CERT_RENEWAL_INTERVAL", raising=False)
    s = Settings()
    # Under the real schedule the fourth attempt lands at 7x the interval;
    # the shipped 7-day window clears that with room to spare.
    assert (7 * s.greffer_cert_renewal_interval
            <= s.greffer_cert_renewal_window_days * 86400)


def test_the_backoff_cap_is_applied_when_it_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is a ceiling on the doubling, and the validator must honour it.

    At a 12h interval the raw delays would be 12h, 24h, 48h (84h total), but
    the worker caps each at 24h, so the fourth attempt actually lands at 60h
    -- inside a 3-day window. A validator that ignored the cap would reject
    this pair as unworkable when the worker handles it fine.
    """
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", "3")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_INTERVAL", "43200")
    s = Settings()
    assert s.greffer_cert_renewal_interval == 43200


def test_a_backoff_deadline_between_ticks_waits_for_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on c79acda: attempts happen on ticks, not on deadlines.

    7h interval, 2-day window. Modelling deadlines gives attempts at 0, 7,
    21 and 45 hours -- inside 48. But the worker only re-examines an
    instance when it wakes: the 24h backoff set at hour 21 comes due at 45,
    the ticks at 28, 35 and 42 all skip it, and the next tick is at 49 --
    an hour after the certificate expired.
    """
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", "2")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_INTERVAL", str(7 * 3600))
    with pytest.raises(ValidationError, match="renewal attempt"):
        Settings()


def test_a_fourth_attempt_exactly_at_expiry_is_not_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 on 8aecb33: equality is not an in-window retry.

    8h interval, 2-day window: attempts at 0, 8, 24 and 48 hours, the last
    landing precisely on notAfter, when the certificate is already invalid.
    """
    monkeypatch.setenv("GREFFER_ID", "x")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_WINDOW_DAYS", "2")
    monkeypatch.setenv("GREFFER_CERT_RENEWAL_INTERVAL", str(8 * 3600))
    with pytest.raises(ValidationError, match="renewal attempt"):
        Settings()
