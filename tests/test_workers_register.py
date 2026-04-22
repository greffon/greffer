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
    assert kwargs["verify"] == settings.greffer_ssl_verify


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
        _fetch_cert(settings)
    assert "timeout" in mock_requests.get.call_args.kwargs
    assert mock_requests.get.call_args.kwargs["timeout"] == 10.0


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
