"""Tests for the async register_worker and its sync helpers.

Helpers are kept synchronous specifically so they can be unit-tested with
plain pytest + mock — no event loop gymnastics.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from app.main import create_app
from app.settings import Settings
from app.workers.register import (
    _fetch_and_store_crl,
    _fetch_cert,
    _install_cert,
    _maybe_install_initial_tunnel_config,
    _post_register,
    _resolve_hostname,
    register_worker,
)


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def test_post_register_passes_correct_payload(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests:
        _post_register(settings, "10.0.0.1", "tok")
    mock_requests.post.assert_called_once()
    url, = mock_requests.post.call_args.args
    kwargs = mock_requests.post.call_args.kwargs
    assert url.endswith(f"/api/greffer/register/{settings.greffer_id}/")
    assert kwargs["json"]["address"] == "10.0.0.1"
    assert kwargs["json"]["token"] == "tok"
    assert kwargs["json"]["protocol"] == settings.greffer_protocol
    # port must be posted as a str — legacy wire format.
    assert kwargs["json"]["port"] == str(settings.greffer_port)
    # version is always sent so the manager can stamp Greffer.version and
    # enforce the per-greffon min_greffer_version compatibility gate.
    assert kwargs["json"]["version"] == settings.greffer_version
    assert kwargs["verify"] == settings.greffer_ssl_verify
    # mode is omitted when settings.greffer_mode is unset — preserves the
    # pre-tunnel-feature behaviour for proxy greffers (manager treats a
    # missing mode as MODE_PROXY default).
    assert "mode" not in kwargs["json"]


def test_post_register_version_defaults_to_app_version(settings: Settings) -> None:
    """With GREFFER_VERSION unset, the register payload carries the worker's
    own ``app.__version__`` — the single source the manager stamps onto
    ``Greffer.version`` for the compat gate."""
    from app import __version__

    # Sanity: the fixture didn't set GREFFER_VERSION, so the default applies.
    assert settings.greffer_version == __version__ == "0.3.3"
    with patch("app.workers.register.requests") as mock_requests:
        _post_register(settings, "10.0.0.1", "tok")
    kwargs = mock_requests.post.call_args.kwargs
    assert kwargs["json"]["version"] == "0.3.3"


def test_post_register_version_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GREFFER_VERSION`` overrides the default (e.g. a build/release stamp);
    the override flows straight into the register payload."""
    from app.settings import Settings, get_settings

    monkeypatch.setenv("GREFFER_ID", "test-greffer-id")
    monkeypatch.setenv("GREFFER_VERSION", "9.9.9-rc1")
    get_settings.cache_clear()
    overridden = Settings()
    assert overridden.greffer_version == "9.9.9-rc1"
    with patch("app.workers.register.requests") as mock_requests:
        _post_register(overridden, "10.0.0.1", "tok")
    kwargs = mock_requests.post.call_args.kwargs
    assert kwargs["json"]["version"] == "9.9.9-rc1"


def test_post_register_includes_mode_when_set(settings: Settings) -> None:
    """Operator flipping a greffer to tunnel mode at the manager (PATCH
    /api/greffer/{id}/mode/) must also set GREFFER_MODE=tunnel here so
    the register payload carries the matching mode and avoids
    400 mode_mismatch on the next poll."""
    settings.greffer_mode = "tunnel"  # type: ignore[misc]
    with patch("app.workers.register.requests") as mock_requests:
        _post_register(settings, "10.0.0.1", "tok")
    kwargs = mock_requests.post.call_args.kwargs
    assert kwargs["json"]["mode"] == "tunnel"


def test_settings_empty_greffer_mode_treated_as_unset(monkeypatch) -> None:
    """env.env documents an empty default ``GREFFER_MODE=`` for operators
    who don't opt into tunnel mode. Without ``env_ignore_empty`` on the
    Settings model, ``Literal["proxy","tunnel"] | None`` would
    ValidationError on the empty string and the greffer wouldn't boot.
    Codex P1 on greffer#23."""
    from app.settings import Settings, get_settings
    monkeypatch.setenv("GREFFER_ID", "test")
    monkeypatch.setenv("GREFFER_MODE", "")
    get_settings.cache_clear()
    s = Settings()
    assert s.greffer_mode is None


def test_fetch_cert_returns_data_on_200(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"certificate": "c", "private_key": "k"}
        mock_requests.get.return_value = mock_response
        assert _fetch_cert(settings, "tok") == {"certificate": "c", "private_key": "k"}


def test_fetch_cert_returns_none_on_non_200(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests:
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_requests.get.return_value = mock_response
        assert _fetch_cert(settings, "tok") is None


def test_fetch_cert_sends_greffer_token_header(settings: Settings) -> None:
    """The cert response carries the private key (and the tunnel client
    config in tunnel mode), so the poll must identify itself: the manager
    authenticates it via ``X-Greffer-Token`` against the token this greffer
    registered with."""
    with patch("app.workers.register.requests") as mock_requests:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests.get.return_value = mock_response
        _fetch_cert(settings, "sekret-token")
    _, kwargs = mock_requests.get.call_args
    assert kwargs["headers"] == {"X-Greffer-Token": "sekret-token"}


def test_install_cert_copies_files(settings: Settings) -> None:
    data = {"certificate": "CERT", "private_key": "KEY"}
    with patch("apps.utils.docker.base.copy_file_into_container") as mock_copy:
        _install_cert(settings, data)
    assert mock_copy.call_count == 2
    mock_copy.assert_any_call(
        settings.docker_nginx_name, "/root", "pem.crt", "CERT"
    )
    mock_copy.assert_any_call(
        settings.docker_nginx_name, "/root", "cert.key", "KEY"
    )


def test_install_cert_optional_ca(settings: Settings) -> None:
    data = {"certificate": "C", "private_key": "K", "issuing_ca": "CA"}
    with patch("apps.utils.docker.base.copy_file_into_container") as mock_copy:
        _install_cert(settings, data)
    assert mock_copy.call_count == 3
    mock_copy.assert_any_call(
        settings.docker_nginx_name, "/root", "ca.pem", "CA"
    )


def test_fetch_and_store_crl_happy_path(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ) as mock_copy:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "CRL-DATA"
        mock_requests.get.return_value = mock_response
        _fetch_and_store_crl(settings)
    mock_copy.assert_called_once_with(
        settings.docker_nginx_name, "/root", "revoked.crl", "CRL-DATA"
    )


def test_fetch_and_store_crl_swallows_exception(settings: Settings) -> None:
    """Any exception is caught and logged — parity with legacy sync_crl."""
    with patch("app.workers.register.requests") as mock_requests:
        mock_requests.get.side_effect = requests.ConnectionError("boom")
        # Must not raise.
        _fetch_and_store_crl(settings)


def test_fetch_and_store_crl_skips_on_non_200(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ) as mock_copy:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_requests.get.return_value = mock_response
        _fetch_and_store_crl(settings)
    mock_copy.assert_not_called()


def test_resolve_hostname_returns_ip() -> None:
    with patch("app.workers.register.socket") as mock_socket:
        mock_socket.gethostname.return_value = "host"
        mock_socket.gethostbyname.return_value = "1.2.3.4"
        assert _resolve_hostname() == "1.2.3.4"


# ---------------------------------------------------------------------------
# Async worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_worker_happy_path(settings: Settings) -> None:
    """Happy path: POST succeeds, GET returns 200, cert installs, CRL
    fetched, worker returns."""
    app = create_app(token="tok", settings=settings)
    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ) as mock_copy:
        post_response = MagicMock()
        post_response.status_code = 200
        mock_requests.post.return_value = post_response

        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {
            "certificate": "CERT",
            "private_key": "KEY",
        }
        crl_response = MagicMock()
        crl_response.status_code = 200
        crl_response.text = "CRL"
        # First GET is cert, second GET is CRL.
        mock_requests.get.side_effect = [cert_response, crl_response]

        await register_worker(app)

    mock_requests.post.assert_called_once()
    # cert install -> pem.crt + cert.key ; CRL install -> revoked.crl = 3 copies
    assert mock_copy.call_count == 3


@pytest.mark.asyncio
async def test_register_worker_retries_post_on_connection_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ConnectionError on POST triggers the 3s retry. Patch asyncio.sleep
    to avoid waiting in tests."""
    app = create_app(token="tok", settings=settings)

    sleeps: list[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _record_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ):
        # Patching the whole `requests` module replaces exception classes
        # with MagicMock children, which aren't real exception classes —
        # the `except (ConnectionError, Timeout):` clause raises TypeError
        # unless we pin the real classes on the mock.
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.Timeout = requests.Timeout
        mock_requests.post.side_effect = [
            requests.ConnectionError(),
            MagicMock(),  # second POST succeeds
        ]
        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {"certificate": "C", "private_key": "K"}
        crl_response = MagicMock()
        crl_response.status_code = 200
        crl_response.text = "CRL"
        mock_requests.get.side_effect = [cert_response, crl_response]

        await register_worker(app)

    assert mock_requests.post.call_count == 2
    # The first sleep must be the register-retry delay.
    assert 3.0 in sleeps


@pytest.mark.asyncio
async def test_register_worker_retries_post_on_timeout(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``requests.Timeout`` is not a ``ConnectionError`` subclass. Verify
    the except clause covers both so a slow manager doesn't crash the
    worker."""
    app = create_app(token="tok", settings=settings)

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ):
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.Timeout = requests.Timeout
        mock_requests.post.side_effect = [
            requests.Timeout(),  # first POST times out
            MagicMock(),  # second POST succeeds
        ]
        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        mock_requests.get.side_effect = [cert_response, crl]

        await register_worker(app)

    assert mock_requests.post.call_count == 2


@pytest.mark.asyncio
async def test_post_register_carries_timeout(settings: Settings) -> None:
    """_post_register must pass ``timeout`` so the thread can't hang on a
    stalled manager."""
    with patch("app.workers.register.requests") as mock_requests:
        _post_register(settings, "10.0.0.1", "tok")
    assert "timeout" in mock_requests.post.call_args.kwargs
    assert mock_requests.post.call_args.kwargs["timeout"] == 10.0


def test_fetch_cert_carries_timeout(settings: Settings) -> None:
    """_fetch_cert must pass ``timeout`` so the cert-poll thread can't
    hang."""
    with patch("app.workers.register.requests") as mock_requests:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_requests.get.return_value = mock_response
        _fetch_cert(settings, "tok")
    assert "timeout" in mock_requests.get.call_args.kwargs
    assert mock_requests.get.call_args.kwargs["timeout"] == 10.0


@pytest.mark.asyncio
async def test_register_worker_survives_cert_poll_transient_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: a transient ConnectionError/Timeout during the cert
    poll (phase 2) must be caught and retried — otherwise the one-shot
    worker terminates and the greffer sits unregistered until process
    restart, even though the initial POST succeeded.
    """
    app = create_app(token="tok", settings=settings)

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ):
        mock_requests.ConnectionError = requests.ConnectionError
        mock_requests.Timeout = requests.Timeout
        mock_requests.post.return_value = MagicMock()

        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        # Sequence: connection error, timeout, success, CRL.
        mock_requests.get.side_effect = [
            requests.ConnectionError("blip"),
            requests.Timeout("slow"),
            cert_response,
            crl,
        ]

        # Must complete without propagating the transient errors.
        await register_worker(app)

    # Two failed polls + one successful cert fetch + one CRL fetch = 4 GETs.
    assert mock_requests.get.call_count == 4


@pytest.mark.asyncio
async def test_register_worker_polls_cert_until_200(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="tok", settings=settings)

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ):
        mock_requests.post.return_value = MagicMock()

        fail = MagicMock()
        fail.status_code = 401
        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        mock_requests.get.side_effect = [fail, fail, success, crl]

        await register_worker(app)

    # 2 failed cert polls + 1 successful + 1 CRL fetch = 4
    assert mock_requests.get.call_count == 4


@pytest.mark.asyncio
async def test_register_worker_uses_token_from_app_state(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token must come from ``app.state.greffer_token``, not the Django
    module-global in ``apps/utils/auth.py``."""
    app = create_app(token="fastapi-specific-token", settings=settings)

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ):
        mock_requests.post.return_value = MagicMock()
        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        mock_requests.get.side_effect = [cert_response, crl]

        await register_worker(app)

    posted_payload = mock_requests.post.call_args.kwargs["json"]
    assert posted_payload["token"] == "fastapi-specific-token"


@pytest.mark.asyncio
async def test_register_worker_falls_back_to_hostname(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When settings.greffer_address is None, resolve via socket."""
    settings.greffer_address = None  # type: ignore[misc]
    app = create_app(token="tok", settings=settings)

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.socket") as mock_socket, patch(
        "app.workers.register.requests"
    ) as mock_requests, patch("apps.utils.docker.base.copy_file_into_container"):
        mock_socket.gethostname.return_value = "h"
        mock_socket.gethostbyname.return_value = "9.9.9.9"
        mock_requests.post.return_value = MagicMock()
        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        mock_requests.get.side_effect = [cert_response, crl]

        await register_worker(app)

    assert mock_requests.post.call_args.kwargs["json"]["address"] == "9.9.9.9"


# ---------------------------------------------------------------------------
# v3 push: initial tunnel client.toml shipped in the cert response
#
# When admin accepts a tunnel-mode greffer's registration, the manager
# embeds the rendered client.toml in the cert response body. The
# register-worker writes it to the shared volume so rathole-client can
# come up immediately. Failure here is non-fatal — the greffer is
# still functional in proxy mode and the next start/stop push retries.
# ---------------------------------------------------------------------------


def test_initial_tunnel_config_writes_when_field_present(
    settings: Settings, tmp_path
) -> None:
    target = tmp_path / "client.toml"
    settings.greffer_tunnel_client_config_path = str(target)
    data = {
        "certificate": "C",
        "private_key": "K",
        "tunnel_client_toml": '[client]\nremote_addr = "x"\n',
    }
    _maybe_install_initial_tunnel_config(settings, data)
    assert target.read_text() == '[client]\nremote_addr = "x"\n'


def test_initial_tunnel_config_skips_when_field_absent(
    settings: Settings, tmp_path
) -> None:
    """Proxy-mode greffer (or v2 manager not pushing the field) — the
    cert response has no ``tunnel_client_toml`` key. Helper is a no-op,
    no file is created, no exception raised."""
    target = tmp_path / "client.toml"
    settings.greffer_tunnel_client_config_path = str(target)
    data = {"certificate": "C", "private_key": "K"}
    _maybe_install_initial_tunnel_config(settings, data)
    assert not target.exists()


def test_initial_tunnel_config_non_string_payload_is_non_fatal(
    settings: Settings, tmp_path
) -> None:
    """A misbehaving / compromised manager could return
    ``tunnel_client_toml`` as something other than a string (dict, list,
    int) — the underlying f.write() would raise TypeError, NOT OSError.
    The non-fatal contract requires catching that too; otherwise the
    register-worker aborts mid-flow.

    Codex P2 on greffer#25."""
    target = tmp_path / "client.toml"
    settings.greffer_tunnel_client_config_path = str(target)
    # Non-string payload — manager bug or hostile peer.
    data = {
        "certificate": "C",
        "private_key": "K",
        "tunnel_client_toml": {"this": "should-be-a-string"},
    }
    with patch("app.workers.register.logger") as mock_logger:
        # Must not raise — register flow continues.
        _maybe_install_initial_tunnel_config(settings, data)

    assert mock_logger.warning.called
    msg = mock_logger.warning.call_args.args[0]
    assert "non-fatal" in msg
    # File not created (write failed before atomicity could complete).
    assert not target.exists()


def test_initial_tunnel_config_failure_is_non_fatal(
    settings: Settings, tmp_path
) -> None:
    """OS-level write failure (e.g. directory missing) must NOT raise —
    register flow continues and eventually completes. The register
    worker log line gets a 'non-fatal' marker so operators searching
    for 'failed' in logs can distinguish this from a hard error.

    We assert on the logger call directly rather than via ``caplog``
    because pytest's ``caplog`` only captures records propagated to
    the root logger, and the greffer logger is a top-level named
    logger (``logging.getLogger("greffer")``) whose propagation
    behaviour depends on sibling test ordering. Patching is
    deterministic across test orderings.
    """
    settings.greffer_tunnel_client_config_path = str(
        tmp_path / "no-such-dir" / "client.toml"
    )
    data = {
        "certificate": "C",
        "private_key": "K",
        "tunnel_client_toml": "[client]\n",
    }
    with patch("app.workers.register.logger") as mock_logger:
        # Must not raise.
        _maybe_install_initial_tunnel_config(settings, data)

    # The warning message structure matters — operators grep for
    # 'non-fatal' to distinguish recoverable greffer-side write errors
    # from hard register failures.
    assert mock_logger.warning.called
    msg = mock_logger.warning.call_args.args[0]
    assert "non-fatal" in msg
