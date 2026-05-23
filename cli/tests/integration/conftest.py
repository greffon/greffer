"""Shared fixtures for greffer CLI integration smoke tests.

Two primary fixtures:

- ``mock_manager`` — a real ``http.server.ThreadingHTTPServer`` bound
  to ``127.0.0.1`` on an ephemeral port. Yields a controller object
  the test uses to script per-request behavior (state-public response
  sequence, register response, cert response, etc.). Exercises the
  real ``httpx`` client layer — bugs in URL construction, header
  handling, and JSON parsing surface here.

- ``mock_docker`` — monkeypatches the ``greffer_cli.compose`` module
  to short-circuit every docker subprocess call. Returns canned
  success results for ``compose_up`` / ``compose_services_running``
  / ``exec_in_greffer_healthz`` / ``exec_nginx_cert_installed``.
  No PATH-prepend shim, no cross-platform .cmd file — pure Python.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from greffer_cli import compose


# --- mock manager ---------------------------------------------------------


@dataclass
class _ManagerController:
    """Per-test handle for scripting the mock manager's behavior."""

    # State sequence returned by GET /api/greffer/{id}/state-public/.
    # The handler walks the list; when exhausted, repeats the last entry.
    # Use ["GREFFER_CREATED", "GREFFER_REGISTERING", "GREFFER_REGISTERED"]
    # for a happy-path acceptance.
    state_sequence: list[str] = field(default_factory=list)

    # Override to make state-public return 404 (greffer doesn't exist on
    # this manager). Triggers the EXIT_GREFFER_NOT_FOUND code path in the
    # CLI.
    state_returns_404: bool = False

    # Recorded requests, for assertions.
    requests: list[tuple[str, str]] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_base_url(self, url: str) -> None:
        self._base_url = url

    def _next_state(self) -> str:
        # Walk the sequence; when exhausted, repeat the last entry so a
        # peg-on-REGISTERING scenario can be expressed as a single-element
        # list ["GREFFER_REGISTERING"] rather than a long repeated list.
        if not self.state_sequence:
            return "GREFFER_CREATED"
        if len(self.state_sequence) == 1:
            return self.state_sequence[0]
        return self.state_sequence.pop(0)


def _make_handler(controller: _ManagerController):
    """Closure binding the handler class to the test's controller."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Silence the default stderr-spam during test runs.
            pass

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            controller.requests.append(("GET", self.path))

            if self.path.startswith("/api/greffer/") and self.path.endswith(
                "/state-public/",
            ):
                if controller.state_returns_404:
                    self._send_json(404, {"detail": "not found"})
                    return
                self._send_json(
                    200, {"state": controller._next_state()},
                )
                return

            # Unknown route — 404 so tests fail loudly on unexpected calls.
            self._send_json(404, {"detail": f"unhandled: {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            controller.requests.append(("POST", self.path))
            # No POST surfaces are exercised by the `up` happy path
            # (register happens inside the greffer container, not from
            # the CLI). 404 keeps the test honest.
            self._send_json(404, {"detail": f"unhandled: {self.path}"})

    return _Handler


@pytest.fixture
def mock_manager():
    """Yield a controller; manage the HTTPServer lifecycle."""
    controller = _ManagerController()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(controller))
    port = server.server_address[1]
    controller.set_base_url(f"http://127.0.0.1:{port}")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield controller
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- mock docker ----------------------------------------------------------


@dataclass
class _DockerController:
    """Per-test handle for scripting the mock docker's behavior."""

    # When False, ``compose_services_running`` returns an empty dict
    # (services not up yet) so the CLI triggers ``compose_up``.
    # The mock then flips this True automatically.
    services_up: bool = False

    # Calls recorded for assertion. Each entry is a (fn_name, args) tuple.
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)


@pytest.fixture
def mock_docker(monkeypatch):
    """Replace the compose module's docker wrappers with no-ops.

    Returns a controller the test can use to flip ``services_up`` or
    inspect ``calls``. Default behavior is the happy-path shape:
    ``compose_services_running`` initially returns empty (forcing a
    ``compose_up`` call), then after that call returns all-services-
    running on subsequent invocations.
    """
    controller = _DockerController()

    def _ok_result() -> compose.CommandResult:
        # Mirror the shape of a successful docker subprocess result.
        # ``ok`` is a derived property (returncode == 0), not a field.
        return compose.CommandResult(returncode=0, stdout="", stderr="")

    def _compose_services_running(compose_file, *, profile=None):
        controller.calls.append(("compose_services_running", (compose_file, profile)))
        if controller.services_up:
            # Tunnel-mode services: greffer + nginx + tunnel-sidecar.
            return {"greffer": True, "nginx": True, "tunnel-sidecar": True}
        return {}

    def _compose_up(compose_file, *, profile=None):
        controller.calls.append(("compose_up", (compose_file, profile)))
        controller.services_up = True
        return _ok_result()

    def _exec_in_greffer_healthz(compose_file):
        controller.calls.append(("exec_in_greffer_healthz", (compose_file,)))
        return _ok_result()

    def _exec_nginx_cert_installed(compose_file):
        controller.calls.append(("exec_nginx_cert_installed", (compose_file,)))
        return _ok_result()

    monkeypatch.setattr(compose, "compose_services_running", _compose_services_running)
    monkeypatch.setattr(compose, "compose_up", _compose_up)
    monkeypatch.setattr(compose, "exec_in_greffer_healthz", _exec_in_greffer_healthz)
    monkeypatch.setattr(
        compose, "exec_nginx_cert_installed", _exec_nginx_cert_installed,
    )

    # Speed up the poll loops — production defaults are 2s which would
    # add real wall-clock per test. Patch the time.sleep used by the
    # state-machine driver to a no-op.
    from greffer_cli import up as _up_mod
    monkeypatch.setattr(_up_mod.time, "sleep", lambda _seconds: None)

    return controller
