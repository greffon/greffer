"""Tests for greffer_cli.compose — subprocess wrapper invariants."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greffer_cli import compose


def test_compose_ps_passes_all_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``docker compose ps`` without ``--all`` hides
    exited/crashed services, so a crashed greffer container disappeared
    from the dict and ``wait_for_compose_running``'s ``all(values)``
    check returned a misleading True. We must pass ``--all``."""
    captured: dict[str, list[str]] = {}

    def fake_run(args, *, timeout=None):
        captured["args"] = list(args)
        return compose.CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compose, "_run", fake_run)
    compose.compose_ps(Path("/tmp/compose.yml"))
    assert "--all" in captured["args"]
    assert "--format" in captured["args"]
    assert "json" in captured["args"]


def test_compose_ps_with_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args, *, timeout=None):
        captured["args"] = list(args)
        return compose.CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compose, "_run", fake_run)
    compose.compose_ps(Path("/tmp/compose.yml"), profile="tunnel")
    args = captured["args"]
    assert "--profile" in args
    assert args[args.index("--profile") + 1] == "tunnel"


def test_compose_services_running_surfaces_exited_as_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``--all`` enabled, a crashed service appears with state=exited.
    That maps to running=False, which the caller correctly treats as
    not-up — instead of silently dropping the row."""
    ndjson = "\n".join([
        json.dumps({"Service": "greffer", "State": "running"}),
        json.dumps({"Service": "nginx", "State": "exited"}),
    ])
    monkeypatch.setattr(
        compose, "compose_ps",
        lambda f, profile=None: compose.CommandResult(
            returncode=0, stdout=ndjson, stderr="",
        ),
    )
    services = compose.compose_services_running(Path("/tmp/compose.yml"))
    assert services == {"greffer": True, "nginx": False}


def test_compose_services_running_warning_then_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a warning line followed by the documented JSON-array
    format (Compose v1 / `--format json`) used to append the array as
    a single list item and crash on item.get(). Now the array is
    flattened correctly."""
    text = (
        "WARN[0000] some compose deprecation notice\n"
        + json.dumps([
            {"Service": "greffer", "State": "running"},
            {"Service": "nginx", "State": "exited"},
        ])
    )
    monkeypatch.setattr(
        compose, "compose_ps",
        lambda f, profile=None: compose.CommandResult(
            returncode=0, stdout=text, stderr="",
        ),
    )
    services = compose.compose_services_running(Path("/tmp/compose.yml"))
    assert services == {"greffer": True, "nginx": False}


def test_compose_services_running_tolerates_warning_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose-plugin warnings printed to stdout used to crash status
    with JSONDecodeError. We now skip unparseable lines."""
    text = (
        "WARN[0000] some compose deprecation notice\n"
        + json.dumps({"Service": "greffer", "State": "running"})
    )
    monkeypatch.setattr(
        compose, "compose_ps",
        lambda f, profile=None: compose.CommandResult(
            returncode=0, stdout=text, stderr="",
        ),
    )
    services = compose.compose_services_running(Path("/tmp/compose.yml"))
    assert services == {"greffer": True}
