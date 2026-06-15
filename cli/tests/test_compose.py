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


# --- Update engine helpers -------------------------------------------

_SAMPLE_COMPOSE = """\
version: "3.8"
name: greffer
services:
  greffer:
    image: greffon/greffer:0.3.3
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - greffon-data:/data
  nginx:
    image: greffon/greffer-nginx:0.3.3
  tunnel-sidecar:
    image: greffon/tunnel-sidecar:0.3.3
    profiles: ["tunnel"]
  other:
    image: postgres:16
volumes:
  greffon-data:
"""


def test_compose_pull_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        compose, "_run",
        lambda args, *, timeout=None: (
            captured.update(args=list(args))
            or compose.CommandResult(0, "", "")
        ),
    )
    compose.compose_pull(
        Path("/tmp/c.yml"), profile="tunnel",
        services=["greffer", "nginx", "tunnel-sidecar"],
    )
    a = captured["args"]
    assert a[:4] == ["docker", "compose", "-f", "/tmp/c.yml"]
    assert "--profile" in a and a[a.index("--profile") + 1] == "tunnel"
    assert a[a.index("pull") + 1:] == ["greffer", "nginx", "tunnel-sidecar"]


def test_set_image_tag_rewrites_all_greffon_images(tmp_path: Path) -> None:
    f = tmp_path / "docker-compose.yml"
    f.write_text(_SAMPLE_COMPOSE, encoding="utf-8")
    old = compose.set_image_tag(f, "0.3.4")
    out = f.read_text(encoding="utf-8")
    # every greffon/* image is now :0.3.4
    assert "image: greffon/greffer:0.3.4" in out
    assert "image: greffon/greffer-nginx:0.3.4" in out
    assert "image: greffon/tunnel-sidecar:0.3.4" in out
    # non-greffon image untouched
    assert "image: postgres:16" in out
    # prior refs returned for rollback
    assert old == {
        "greffon/greffer": "greffon/greffer:0.3.3",
        "greffon/greffer-nginx": "greffon/greffer-nginx:0.3.3",
        "greffon/tunnel-sidecar": "greffon/tunnel-sidecar:0.3.3",
    }


def test_set_image_tag_rewrites_digest_pinned_ref(tmp_path: Path) -> None:
    f = tmp_path / "docker-compose.yml"
    f.write_text(
        "services:\n  greffer:\n"
        "    image: greffon/greffer@sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    old = compose.set_image_tag(f, "0.3.5")
    assert "image: greffon/greffer:0.3.5" in f.read_text(encoding="utf-8")
    assert old["greffon/greffer"] == "greffon/greffer@sha256:" + "a" * 64


def test_set_image_refs_restores_per_repo(tmp_path: Path) -> None:
    f = tmp_path / "docker-compose.yml"
    f.write_text(_SAMPLE_COMPOSE, encoding="utf-8")
    compose.set_image_tag(f, "0.3.4")
    # rollback: pin greffer to a digest, restore the others to their tag
    compose.set_image_refs(f, {
        "greffon/greffer": "greffon/greffer@sha256:" + "b" * 64,
        "greffon/greffer-nginx": "greffon/greffer-nginx:0.3.3",
        "greffon/tunnel-sidecar": "greffon/tunnel-sidecar:0.3.3",
    })
    out = f.read_text(encoding="utf-8")
    assert "image: greffon/greffer@sha256:" + "b" * 64 in out
    assert "image: greffon/greffer-nginx:0.3.3" in out
    assert "image: greffon/tunnel-sidecar:0.3.3" in out
    assert "image: postgres:16" in out  # untouched (absent from refs)


def test_data_volume_is_named(tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text(_SAMPLE_COMPOSE, encoding="utf-8")
    assert compose.data_volume_is_named(f) is True


@pytest.mark.parametrize(
    "mount, expected",
    [
        ("      - greffon-data:/data", True),
        ("      - greffon-data:/data:rw", True),
        ("      - /srv/greffer:/data", False),   # absolute bind
        ("      - ./data:/data", False),         # relative bind
        ("      - ~/data:/data", False),         # home bind
        ("      - greffon-data:/other", False),  # not /data
    ],
)
def test_data_volume_named_vs_bind(tmp_path: Path, mount: str, expected: bool) -> None:
    f = tmp_path / "c.yml"
    f.write_text("services:\n  greffer:\n    volumes:\n" + mount + "\n", encoding="utf-8")
    assert compose.data_volume_is_named(f) is expected


def test_data_volume_absent(tmp_path: Path) -> None:
    f = tmp_path / "c.yml"
    f.write_text("services:\n  greffer:\n    image: greffon/greffer:0.3.3\n", encoding="utf-8")
    assert compose.data_volume_is_named(f) is False


def test_exec_in_greffer_readyz_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        compose, "_run",
        lambda args, *, timeout=None: (
            captured.update(args=list(args))
            or compose.CommandResult(0, '{"id":"x","status":"ready","reasons":[]}', "")
        ),
    )
    compose.exec_in_greffer_readyz(Path("/tmp/c.yml"))
    a = captured["args"]
    assert a[:7] == [
        "docker", "compose", "-f", "/tmp/c.yml", "exec", "-T", "greffer",
    ]
    probe = a[-1]
    assert "/readyz" in probe
    assert "X-GREFFON-TOKEN" in probe
    assert "/data/.greffer-token" in probe


def test_image_id_and_container_image_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=None):
        calls.append(list(args))
        if args[:3] == ["docker", "image", "inspect"]:
            return compose.CommandResult(0, "sha256:deadbeef\n", "")
        if args[:5] == ["docker", "compose", "-f", "/tmp/c.yml", "ps"]:
            return compose.CommandResult(0, "container123\n", "")
        if args[:2] == ["docker", "inspect"]:
            return compose.CommandResult(0, "sha256:cafe\n", "")
        return compose.CommandResult(1, "", "unexpected")

    monkeypatch.setattr(compose, "_run", fake_run)
    assert compose.image_id("greffon/greffer:0.3.4") == "sha256:deadbeef"
    assert compose.container_image_id(Path("/tmp/c.yml"), "greffer") == "sha256:cafe"
