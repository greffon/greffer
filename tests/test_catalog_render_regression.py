"""Catalog render regression oracle.

The compose body is rendered by ``create_compose`` through a Jinja template.
Any change to that render (the sandbox swap, the undefined policy, a
round-trip fix) touches the exact code path every existing deployment shares,
so the merge gate for those changes is this: render every catalog entry and
prove the bytes did not move.

Snapshots live in ``tests/snapshots/catalog_render/``. Regenerate deliberately
with ``CATALOG_RENDER_UPDATE=1 pytest tests/test_catalog_render_regression.py``
and review the diff -- a surprise there is the point of the harness.

Determinism matters more than fidelity here: the fixture pins ``port_host``
and the instance id rather than allocating, because a snapshot that moves on
its own proves nothing. Port allocation is upstream of the render and is not
what these tests guard.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
import yaml
from unittest import mock

from apps.utils.docker import compose as compose_mod
from apps.utils.greffon import repository

_HERE = pathlib.Path(__file__).resolve().parent
_SNAP_DIR = _HERE / "snapshots" / "catalog_render"
_UPDATE = os.getenv("CATALOG_RENDER_UPDATE") == "1"


def _catalog_root() -> pathlib.Path | None:
    """Find the catalog, or return None.

    Walk every ancestor rather than guessing a fixed depth: a direct
    ``greffer/`` checkout, a ``greffer-worktrees/<x>`` worktree and a CI
    checkout all sit at different depths, and a hard-coded range silently
    finds nothing -- which would skip this whole oracle while still reporting
    green. ``GREFFON_CATALOG_DIR`` overrides for layouts we cannot guess."""
    env = os.getenv("GREFFON_CATALOG_DIR")
    if env:
        p = pathlib.Path(env)
        return p if p.is_dir() else None
    for ancestor in _HERE.parents:
        cand = ancestor / "greffon-catalog"
        if (cand / "_template").is_dir() or list(cand.glob("*/*/docker-compose.yml"))[:1]:
            return cand
    return None


def _entries():
    root = _catalog_root()
    if root is None:
        return []
    out = []
    for compose_path in sorted(root.glob("*/*/docker-compose.yml")):
        version_dir = compose_path.parent
        name = f"{version_dir.parent.name}/{version_dir.name}"
        if version_dir.parent.name.startswith("_"):
            continue
        out.append((name, compose_path))
    return out


_ENTRIES = _entries()


def _configurations_for(version_dir: pathlib.Path):
    """Feed each config its catalog default, the way a freshly-created
    instance does before the user edits anything."""
    meta_path = version_dir / "metadata.json"
    if not meta_path.is_file():
        return []
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return []
    configs = []
    for entry in meta.get("configurations", []) or []:
        configs.append({
            "value": entry.get("default_value") or {},
            "destinations": entry.get("destinations") or [],
        })
    return configs


def _greffon_info(instance_id: str, version_dir: pathlib.Path):
    """The START-REQUEST shape (what the manager POSTs), not the derived
    greffon_info: get_greffon_info builds the ports/volumes/networks itself,
    which is the point of routing through it."""
    return {
        "id": instance_id,
        "cert": {"certificate": "-----BEGIN CERTIFICATE-----\nsnapshot\n"
                                "-----END CERTIFICATE-----\n",
                 "private_key": "-----BEGIN PRIVATE KEY-----\nsnapshot\n"
                                "-----END PRIVATE KEY-----\n"},
        "configurations": _configurations_for(version_dir),
        # Field names are the manager's contract (SMTPConfigSerializer in
        # apps/integrations/types/smtp.py): host, port, username, password,
        # from_address, tls_mode. Getting this wrong is not cosmetic -- 15
        # catalog composes reference {{ smtp.username }}, and a fixture that
        # spells it "user" renders them EMPTY, so the snapshots would bless an
        # impossible configured-SMTP state and never exercise interpolation.
        "integrations": {
            "smtp": {
                "host": "smtp.example.test",
                "port": 587,
                "username": "mailer@example.test",
                "password": "pw",
                "from_address": "noreply@example.test",
                "tls_mode": "starttls",
            },
        },
    }


def _render(compose_path: pathlib.Path, tmp_path: pathlib.Path) -> str:
    """Drive the REAL start-flow render and return the rendered compose, or a
    stable marker describing how it failed.

    The order here mirrors app/routers/controller.py exactly:
    get_greffon_info -> build_render_context -> get_compose_template ->
    apply_configuration -> create_compose. Calling create_compose alone would
    snapshot raw catalog ports and volumes with no greffon_nginx sidecar and
    no config destinations applied -- i.e. not the bytes a deployment actually
    renders, so a regression in the real input could pass this gate.

    Only host-port allocation is stubbed, because it probes real sockets and a
    snapshot that moves on its own proves nothing. Everything else is
    production code. A catalog entry that cannot render today is itself a fact
    worth pinning."""
    raw = compose_path.read_text()
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return f"<<UNPARSEABLE {type(exc).__name__}>>"
    if not isinstance(parsed, dict) or "services" not in parsed:
        return "<<NO SERVICES KEY>>"

    instance_id = "regress-" + compose_path.parent.parent.name.replace("_", "-")
    info_seed = _greffon_info(instance_id, compose_path.parent)

    prev = os.environ.get("GREFFON_PATH")
    os.environ["GREFFON_PATH"] = str(tmp_path)
    try:
        with mock.patch(
            "apps.utils.greffon.repository.get_free_ports",
            side_effect=lambda host="127.0.0.1", numbers=1, protocol="tcp": (
                list(range(20000, 20000 + numbers))),
        ):
            info = repository.get_greffon_info(parsed, info_seed)
        # The manager assigns each port its public URL and sends it in the
        # start request; get_greffon_info leaves url=None without it, and
        # instance_url is derived from ports[0].url. Assign deterministically
        # so the snapshot pins real URL interpolation rather than a fallback.
        for idx, port in enumerate(info.get("ports", [])):
            port["url"] = f"https://{instance_id}-{idx}.my.example.test"
        compose_mod.build_render_context(info)
        template = compose_mod.get_compose_template(parsed, info)
        compose_mod.apply_configuration(info, parsed)
        compose_mod.create_compose(template, info)
    except Exception as exc:  # noqa: BLE001 -- pinning today's failure modes
        return f"<<RENDER FAILED {type(exc).__name__}>>"
    finally:
        # Never leak GREFFON_PATH out of this helper: the suite shares one
        # process and test_settings.py asserts the unset default.
        if prev is None:
            os.environ.pop("GREFFON_PATH", None)
        else:
            os.environ["GREFFON_PATH"] = prev
    written = tmp_path / instance_id / "docker-compose.yml"
    if not written.is_file():
        return "<<NO OUTPUT WRITTEN>>"
    return written.read_text()


@pytest.mark.skipif(not _ENTRIES, reason="greffon-catalog checkout not found")
@pytest.mark.parametrize("name,compose_path", _ENTRIES,
                         ids=[n for n, _ in _ENTRIES])
def test_catalog_entry_render_is_unchanged(name, compose_path, tmp_path):
    rendered = _render(compose_path, tmp_path)
    snap = _SNAP_DIR / (name.replace("/", "__") + ".snap")
    if _UPDATE:
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(rendered)
        pytest.skip(f"snapshot written for {name}")
    if not snap.is_file():
        pytest.fail(
            f"no snapshot for {name}. Generate with "
            f"CATALOG_RENDER_UPDATE=1 and review the diff before committing.")
    assert rendered == snap.read_text(), (
        f"render changed for {name}. If this change is intended, regenerate "
        f"with CATALOG_RENDER_UPDATE=1 and review every moved byte.")


def test_the_oracle_actually_covers_the_catalog():
    """A harness that silently covers nothing is worse than none: it reads as
    proof.

    This test deliberately does NOT skip when the catalog is missing. A skip
    here is indistinguishable from green, and the failure mode it would hide is
    the whole suite gating nothing -- which is exactly what happens in a CI job
    that checks out only the greffer repo. Set GREFFON_CATALOG_OPTIONAL=1 to
    opt out deliberately, and point GREFFON_CATALOG_DIR at a pinned catalog
    checkout in CI."""
    if not _ENTRIES and os.getenv("GREFFON_CATALOG_OPTIONAL") == "1":
        pytest.skip("catalog absent and explicitly marked optional")
    assert _ENTRIES, (
        "no greffon-catalog checkout found, so the catalog render oracle "
        "covered NOTHING. Point GREFFON_CATALOG_DIR at a catalog checkout "
        "(CI must fetch one), or set GREFFON_CATALOG_OPTIONAL=1 to accept "
        "the reduced coverage on purpose.")
    assert len(_ENTRIES) >= 25, (
        f"only {len(_ENTRIES)} catalog entries discovered; the oracle is "
        f"supposed to cover the whole catalog (30 at time of writing)")
