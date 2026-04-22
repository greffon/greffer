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
    _check_secure_bootstrap,
    _client_auth,
    _fetch_and_store_crl,
    _fetch_cert,
    _install_cert,
    _post_register,
    _resolve_hostname,
    _validate_cert_response,
    _write_local_cert,
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
    # No cert material on disk → bootstrap path (verify=True, no cert
    # kwarg). Post-registration mTLS is covered in the _client_auth tests.
    assert kwargs["verify"] is True
    assert "cert" not in kwargs


def test_fetch_cert_returns_data_on_200(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"certificate": "c", "private_key": "k"}
        mock_requests.get.return_value = mock_response
        assert _fetch_cert(settings) == {"certificate": "c", "private_key": "k"}


def test_fetch_cert_returns_none_on_non_200(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests:
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_requests.get.return_value = mock_response
        assert _fetch_cert(settings) is None


def test_install_cert_copies_files(settings: Settings, tmp_path) -> None:
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
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


def test_install_cert_optional_ca(settings: Settings, tmp_path) -> None:
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    data = {"certificate": "C", "private_key": "K", "issuing_ca": "CA"}
    with patch("apps.utils.docker.base.copy_file_into_container") as mock_copy:
        _install_cert(settings, data)
    assert mock_copy.call_count == 3
    mock_copy.assert_any_call(
        settings.docker_nginx_name, "/root", "ca.pem", "CA"
    )


def test_install_cert_writes_locally_with_key_before_cert(
    settings: Settings, tmp_path
) -> None:
    """The local cert+key+ca are written to ``greffer_cert_dir`` so this
    process can present the cert on outbound calls. Key lands before cert
    so ``_client_auth``'s "cert exists" precondition implies "key exists"
    — no half-registered window where a monitor tick sees only half of
    the pair."""
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]

    writes: list[str] = []

    def _record_write(_settings, file_name, _content, mode=0o644):
        writes.append(file_name)

    with patch("app.workers.register._write_local_cert", side_effect=_record_write), \
         patch("apps.utils.docker.base.copy_file_into_container"):
        _install_cert(settings, {
            "certificate": "C",
            "private_key": "K",
            "issuing_ca": "CA",
        })

    assert writes == ["cert.key", "pem.crt", "ca.pem"]


def test_install_cert_key_written_with_0o600(
    settings: Settings, tmp_path
) -> None:
    """The private key must not be world-readable even inside an
    otherwise-single-tenant container."""
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    modes: dict[str, int] = {}

    def _record_mode(_settings, file_name, _content, mode=0o644):
        modes[file_name] = mode

    with patch("app.workers.register._write_local_cert", side_effect=_record_mode), \
         patch("apps.utils.docker.base.copy_file_into_container"):
        _install_cert(settings, {"certificate": "C", "private_key": "K"})

    assert modes["cert.key"] == 0o600
    assert modes["pem.crt"] == 0o644


def test_fetch_and_store_crl_happy_path(settings: Settings) -> None:
    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ) as mock_copy, patch("app.workers.register._write_local_cert"):
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
    ) as mock_copy, patch("app.workers.register._write_local_cert"):
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
    ) as mock_copy, patch("app.workers.register._write_local_cert"):
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
    ), patch("app.workers.register._write_local_cert"):
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
    ), patch("app.workers.register._write_local_cert"):
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
        _fetch_cert(settings)
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
    ), patch("app.workers.register._write_local_cert"):
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
    ), patch("app.workers.register._write_local_cert"):
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
    ), patch("app.workers.register._write_local_cert"):
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
    ) as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ), patch("app.workers.register._write_local_cert"):
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
# mTLS helpers
# ---------------------------------------------------------------------------


def test_client_auth_bootstrap_when_no_cert_material(settings: Settings, tmp_path) -> None:
    """Before registration completes, the cert dir is empty — fall back
    to system-CA verification with no client cert."""
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    auth = _client_auth(settings)
    assert auth == {"verify": True}


def test_client_auth_mtls_when_all_material_present(settings: Settings, tmp_path) -> None:
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    (tmp_path / "pem.crt").write_text("CERT")
    (tmp_path / "cert.key").write_text("KEY")
    (tmp_path / "ca.pem").write_text("CA")
    auth = _client_auth(settings)
    assert auth == {
        "verify": str(tmp_path / "ca.pem"),
        "cert": (str(tmp_path / "pem.crt"), str(tmp_path / "cert.key")),
    }


def test_client_auth_presents_cert_when_ca_missing(settings: Settings, tmp_path) -> None:
    """``issuing_ca`` is optional in the cert response. When the CA isn't
    on disk but cert+key are, still present the client cert (fall back
    to system-CA verification)."""
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    (tmp_path / "pem.crt").write_text("CERT")
    (tmp_path / "cert.key").write_text("KEY")
    auth = _client_auth(settings)
    assert auth == {
        "verify": True,
        "cert": (str(tmp_path / "pem.crt"), str(tmp_path / "cert.key")),
    }


def test_check_secure_bootstrap_allows_https(settings: Settings) -> None:
    settings.greffon_base_server = "https://api.greffon.io"  # type: ignore[misc]
    _check_secure_bootstrap(settings)  # must not raise


def test_check_secure_bootstrap_refuses_http_without_opt_in(settings: Settings) -> None:
    settings.greffon_base_server = "http://host.docker.internal:8000"  # type: ignore[misc]
    settings.greffer_allow_insecure_bootstrap = False  # type: ignore[misc]
    with pytest.raises(RuntimeError) as exc:
        _check_secure_bootstrap(settings)
    assert "GREFFER_ALLOW_INSECURE_BOOTSTRAP" in str(exc.value)


def test_check_secure_bootstrap_allows_http_with_opt_in(settings: Settings) -> None:
    settings.greffon_base_server = "http://host.docker.internal:8000"  # type: ignore[misc]
    settings.greffer_allow_insecure_bootstrap = True  # type: ignore[misc]
    _check_secure_bootstrap(settings)  # warns but does not raise


def test_write_local_cert_is_atomic(settings: Settings, tmp_path) -> None:
    """Writes go to <path>.tmp first, then rename — no partial PEM is
    ever readable at the final path."""
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    _write_local_cert(settings, "pem.crt", "CERT_BODY", mode=0o600)
    final = tmp_path / "pem.crt"
    assert final.read_text() == "CERT_BODY"
    # No stray .tmp left behind.
    assert not (tmp_path / "pem.crt.tmp").exists()
    # Mode honored.
    import stat
    assert stat.S_IMODE(final.stat().st_mode) == 0o600


def test_validate_cert_response_accepts_minimal_shape() -> None:
    _validate_cert_response({"certificate": "C", "private_key": "K"})


def test_validate_cert_response_rejects_missing_fields() -> None:
    with pytest.raises(ValueError):
        _validate_cert_response({"certificate": "C"})
    with pytest.raises(ValueError):
        _validate_cert_response({"private_key": "K"})
    with pytest.raises(ValueError):
        _validate_cert_response({"certificate": "", "private_key": "K"})


@pytest.mark.asyncio
async def test_register_worker_sets_registered_event(
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After successful registration + cert install, monitor_worker's
    gate opens."""
    settings.greffer_cert_dir = tmp_path  # type: ignore[misc]
    app = create_app(token="tok", settings=settings)
    assert not app.state.registered.is_set()

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ), patch("app.workers.register._write_local_cert"):
        mock_requests.post.return_value = MagicMock()
        cert_response = MagicMock()
        cert_response.status_code = 200
        cert_response.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        mock_requests.get.side_effect = [cert_response, crl]

        await register_worker(app)

    assert app.state.registered.is_set()


@pytest.mark.asyncio
async def test_register_worker_retries_on_malformed_cert_response(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 with missing required fields must not raise KeyError and
    kill the worker — log and retry until the response is well-formed."""
    app = create_app(token="t", settings=settings)

    async def _noop_sleep(_s: float) -> None:
        return

    monkeypatch.setattr("app.workers.register.asyncio.sleep", _noop_sleep)

    with patch("app.workers.register.requests") as mock_requests, patch(
        "apps.utils.docker.base.copy_file_into_container"
    ), patch("app.workers.register._write_local_cert"):
        mock_requests.post.return_value = MagicMock()
        bad = MagicMock()
        bad.status_code = 200
        bad.json.return_value = {"unexpected": "payload"}
        good = MagicMock()
        good.status_code = 200
        good.json.return_value = {"certificate": "C", "private_key": "K"}
        crl = MagicMock()
        crl.status_code = 200
        crl.text = "CRL"
        mock_requests.get.side_effect = [bad, good, crl]

        await register_worker(app)

    # 1 malformed + 1 good cert fetch + 1 CRL = 3 GETs
    assert mock_requests.get.call_count == 3
    assert app.state.registered.is_set()


@pytest.mark.asyncio
async def test_register_worker_refuses_insecure_bootstrap_without_opt_in(
    settings: Settings,
) -> None:
    settings.greffon_base_server = "http://host.docker.internal:8000"  # type: ignore[misc]
    settings.greffer_allow_insecure_bootstrap = False  # type: ignore[misc]
    app = create_app(token="t", settings=settings)

    with pytest.raises(RuntimeError):
        await register_worker(app)
