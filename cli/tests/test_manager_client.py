"""Tests for greffer_cli.manager_client — exception normalization + polling."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from greffer_cli import manager_client


class _StubResponse:
    def __init__(self, status_code: int, body: Any = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _stub_get(response_or_exc):
    """Build a fake httpx.get that returns the given response or raises an exc."""
    def _get(url: str, *, timeout: float) -> _StubResponse:
        if isinstance(response_or_exc, Exception):
            raise response_or_exc
        return response_or_exc
    return _get


def test_fetch_state_200_returns_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", _stub_get(_StubResponse(200, {"state": "GREFFER_CREATED"})))
    out = manager_client.fetch_state("https://m", "abc")
    assert out.state == "GREFFER_CREATED"


def test_fetch_state_404_raises_greffer_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", _stub_get(_StubResponse(404)))
    with pytest.raises(manager_client.GrefferNotFound):
        manager_client.fetch_state("https://m", "abc")


def test_fetch_state_429_raises_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "get",
        _stub_get(_StubResponse(429, headers={"retry-after": "7"})),
    )
    with pytest.raises(manager_client._RateLimited) as exc_info:
        manager_client.fetch_state("https://m", "abc")
    assert exc_info.value.retry_after == 7.0


@pytest.mark.parametrize("status", [500, 502, 503, 504, 418])
def test_fetch_state_non_200_normalized_to_manager_unreachable(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    """Regression: a 5xx from the manager used to leak as httpx.HTTPStatusError
    and abort polling. It now surfaces as ManagerUnreachable, which
    poll_state retries on the back-off schedule."""
    monkeypatch.setattr(httpx, "get", _stub_get(_StubResponse(status)))
    with pytest.raises(manager_client.ManagerUnreachable):
        manager_client.fetch_state("https://m", "abc")


def test_fetch_state_transport_error_raises_manager_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", _stub_get(httpx.ConnectError("refused")))
    with pytest.raises(manager_client.ManagerUnreachable):
        manager_client.fetch_state("https://m", "abc")


def test_fetch_state_non_json_body_raises_manager_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx, "get",
        _stub_get(_StubResponse(200, body=ValueError("bad json"))),
    )
    with pytest.raises(manager_client.ManagerUnreachable):
        manager_client.fetch_state("https://m", "abc")


def test_poll_state_recovers_from_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a transient 5xx must NOT abort poll_state — operators
    routinely see a 502 from a load balancer mid-deploy. The loop should
    back off and resume on success."""
    monkeypatch.setattr(manager_client.time, "sleep", lambda _: None)

    calls = {"n": 0}

    def fake_fetch(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise manager_client.ManagerUnreachable("simulated 502")
        return manager_client.StatePublic(state="GREFFER_REGISTERED")

    monkeypatch.setattr(manager_client, "fetch_state", fake_fetch)

    gen = manager_client.poll_state("https://m", "abc")
    first = next(gen)
    assert first.state == "GREFFER_REGISTERED"
    assert calls["n"] == 3  # two failures then a success


def test_poll_state_propagates_greffer_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal verdict (greffer ID unknown) must NOT be swallowed by the
    transient-error retry path."""
    monkeypatch.setattr(manager_client.time, "sleep", lambda _: None)

    def fake_fetch(*_args, **_kwargs):
        raise manager_client.GrefferNotFound("abc")

    monkeypatch.setattr(manager_client, "fetch_state", fake_fetch)

    gen = manager_client.poll_state("https://m", "abc")
    with pytest.raises(manager_client.GrefferNotFound):
        next(gen)
