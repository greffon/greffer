"""Black-box smoke tests for ``greffer up``.

These exercise the full Typer CLI entrypoint against:
  - real httpx requests to a real (in-process) HTTP server
  - monkeypatched compose-module wrappers (no real docker calls)

They sit one layer above the function-level unit tests in
``../test_state_machine.py`` and ``../test_up.py``. The marginal
coverage:

  - Arg parsing (``CliRunner.invoke`` runs the real Typer registry)
  - Exit-code contract (operator sees these — they're the API)
  - stdout/stderr content (the operator-facing copy we ship)
  - httpx client layer (URL construction, headers, JSON parsing)

What they DON'T cover (intentional):
  - Real Docker daemon — would require DinD or matrix per OS
  - install.sh / install.ps1 shell logic — needs real OS provisioning
"""

from __future__ import annotations

import uuid

from typer.testing import CliRunner

from greffer_cli import up as up_mod
from greffer_cli.main import app


# mix_stderr=True (default) combines stdout + stderr into result.output,
# which is what we want here — the tests assert against operator-visible
# messages regardless of which stream the CLI used. The CLI's stream
# discipline (errors to stderr, normal output to stdout) is already
# exercised by the unit tests; these integration tests focus on content.
runner = CliRunner()


def _invoke_up(
    *,
    manager_url: str,
    config_dir,
    greffer_id: str | None = None,
    timeout: int = 2,
    mode: str = "tunnel",
):
    """Single canonical ``greffer up`` invocation for tests.

    ``timeout=2`` keeps timeout-path tests fast — combined with the
    ``time.sleep = no-op`` patch from ``mock_docker``, the poll loops
    walk through their full timeout budget in well under a second.
    """
    args = [
        "up",
        "--id", greffer_id or str(uuid.uuid4()),
        "--manager", manager_url,
        "--config-dir", str(config_dir),
        "--mode", mode,
        "--timeout", str(timeout),
    ]
    return runner.invoke(app, args)


# --- Happy path -----------------------------------------------------------


def test_up_happy_path_reaches_connected(
    tmp_path, mock_manager, mock_docker,
):
    """Manager state walks CREATED → REGISTERING → REGISTERED.

    Compose comes up, healthz returns 200, the CLI exits 0 and prints
    the Connected message. The operator-visible contract end-to-end.
    """
    greffer_id = str(uuid.uuid4())
    mock_manager.state_sequence = [
        "GREFFER_CREATED",
        "GREFFER_REGISTERING",
        "GREFFER_REGISTERED",
    ]

    result = _invoke_up(
        manager_url=mock_manager.base_url,
        config_dir=tmp_path,
        greffer_id=greffer_id,
        timeout=10,
    )

    assert result.exit_code == 0, (
        f"non-zero exit: code={result.exit_code}\n"
        f"output:\n{result.output}"
    )
    # The operator-facing transitions all reached.
    assert "Starting" in result.output
    assert "Registering" in result.output
    assert "Awaiting cert" in result.output
    assert "Connected" in result.output
    # Compose was actually invoked (not just polled-as-already-up).
    fn_names = [c[0] for c in mock_docker.calls]
    assert "compose_up" in fn_names
    assert "compose_services_running" in fn_names
    # The manager was actually hit on its state-public endpoint.
    state_polls = [
        path for method, path in mock_manager.requests
        if method == "GET" and path.endswith("/state-public/")
    ]
    assert len(state_polls) >= 1
    assert greffer_id in state_polls[0]


# --- Sad path: timeout in Registering -------------------------------------


def test_up_pegs_on_registering_returns_timeout_exit(
    tmp_path, mock_manager, mock_docker,
):
    """Manager never advances past REGISTERING — admin never accepts.

    CLI should hit the per-state timeout, exit with
    ``EXIT_TIMEOUT_REGISTERING``, and print the manager-UI hint that
    points the operator at the Greffers page (this is the message we
    spent the recent refactor making honest — guard it doesn't
    regress to printing a raw POST URL).
    """
    greffer_id = str(uuid.uuid4())
    mock_manager.state_sequence = ["GREFFER_REGISTERING"]  # peg forever

    result = _invoke_up(
        manager_url=mock_manager.base_url,
        config_dir=tmp_path,
        greffer_id=greffer_id,
        timeout=1,
    )

    assert result.exit_code == up_mod.EXIT_TIMEOUT_REGISTERING, (
        f"expected EXIT_TIMEOUT_REGISTERING ({up_mod.EXIT_TIMEOUT_REGISTERING}), "
        f"got {result.exit_code}\noutput:\n{result.output}"
    )
    # The honest-banner regression guard — operator should see the
    # greffer ID and a manager-UI pointer, not a raw POST URL.
    assert greffer_id in result.output
    assert "register/accept/" not in result.output, (
        "raw POST URL leaked into operator output — the "
        "feat/registering-message-points-at-manager-ui refactor regressed"
    )


# --- Sad path: greffer UUID unknown to manager ----------------------------


def test_up_greffer_not_found_returns_distinct_exit(
    tmp_path, mock_manager, mock_docker,
):
    """state-public returns 404 — UUID doesn't exist on this manager.

    CLI should NOT propagate the exception as a traceback (that's the
    bug the ``EXIT_GREFFER_NOT_FOUND`` branch in ``run_state_machine``
    closed). Should exit with the distinct code and print the typo /
    wrong-environment / wrong-UUID hint.
    """
    greffer_id = str(uuid.uuid4())
    mock_manager.state_returns_404 = True

    result = _invoke_up(
        manager_url=mock_manager.base_url,
        config_dir=tmp_path,
        greffer_id=greffer_id,
        timeout=2,
    )

    assert result.exit_code == up_mod.EXIT_GREFFER_NOT_FOUND, (
        f"expected EXIT_GREFFER_NOT_FOUND ({up_mod.EXIT_GREFFER_NOT_FOUND}), "
        f"got {result.exit_code}\noutput:\n{result.output}"
    )
    # The hint mentions the manager URL the operator gave, the greffer
    # ID, and points at the two likely root causes.
    assert mock_manager.base_url in result.output
    assert greffer_id in result.output
    assert "manager" in result.output.lower()
    # No Python traceback leaked.
    assert "Traceback" not in result.output
