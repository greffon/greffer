"""Tests for the v2 socket-only recreate primitives (updater.recreate).

docker is invoked through ``greffer_cli.compose._run``, monkeypatched here so
there is no real docker / cosign / registry. Focus: stack discovery by compose
service label (HLD section 4), verify-then-pull (pull-by-digest then retag
:latest, sections 3 + 13), and the :previous / dangling-prune primitives,
including the fail-closed paths.
"""

from __future__ import annotations

import pytest

from greffer_cli import compose
from greffer_cli.updater import provenance, recreate

_D = "sha256:" + "a" * 64


def _ok(out: str = "") -> compose.CommandResult:
    return compose.CommandResult(0, out, "")


def _fail(err: str = "boom") -> compose.CommandResult:
    return compose.CommandResult(1, "", err)


# --- discovery (HLD section 4) --------------------------------------

def _make_discover_fake(
    *, greffer_ids: str = "gid", project: str = "greffer",
    listing: str = "gid\tgreffer\nnid\tnginx\nsid\ttunnel-sidecar\n",
    ps1_ok: bool = True, inspect_ok: bool = True, ps2_ok: bool = True,
):
    """A compose._run double that answers the three discovery docker calls:
    find-greffer-by-service-label, project-of, list-project-containers."""
    def fake(args, *, timeout=None):
        sub = args[1:]
        if sub[0] == "ps" and "label=com.docker.compose.service=greffer" in sub:
            return _ok(greffer_ids + "\n") if ps1_ok else _fail()
        if sub[0] == "inspect" and any("com.docker.compose.project" in a for a in sub):
            return _ok(project + "\n") if inspect_ok else _fail()
        if sub[0] == "ps" and any(
            a.startswith("label=com.docker.compose.project=") for a in sub
        ):
            return _ok(listing) if ps2_ok else _fail()
        return _ok()
    return fake


def test_discover_stack_orders_and_maps_repos(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake())
    stack = recreate.discover_stack()
    # ordered per RECREATE_ORDER (nginx, greffer, tunnel-sidecar), NOT listing order
    assert [c.service for c in stack] == ["nginx", "greffer", "tunnel-sidecar"]
    # repo mapped from the SERVICE label, never the image name
    assert [c.repo for c in stack] == [
        "greffon/greffer-nginx", "greffon/greffer", "greffon/tunnel-sidecar"]
    assert [c.container_id for c in stack] == ["nid", "gid", "sid"]


def test_discover_no_greffer_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(greffer_ids=""))
    assert recreate.discover_stack() == []


def test_discover_ps_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(ps1_ok=False))
    assert recreate.discover_stack() == []


def test_discover_no_project_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(inspect_ok=False))
    assert recreate.discover_stack() == []


def test_discover_listing_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(compose, "_run", _make_discover_fake(ps2_ok=False))
    assert recreate.discover_stack() == []


def test_discover_ignores_unknown_services(monkeypatch):
    listing = "gid\tgreffer\nxid\tsome-other\nnid\tnginx\n"
    monkeypatch.setattr(compose, "_run", _make_discover_fake(listing=listing))
    stack = recreate.discover_stack()
    # 'some-other' is not in SERVICE_REPO -> dropped; sidecar absent -> not present
    assert [c.service for c in stack] == ["nginx", "greffer"]


# --- verify-then-pull (HLD sections 3 + 13) -------------------------

def _patch_provenance(monkeypatch, *, digest=_D, cosign_ok=True):
    monkeypatch.setattr(provenance, "resolve_digest", lambda ref: digest)
    monkeypatch.setattr(provenance, "cosign_verify", lambda repo, d, **k: cosign_ok)


def test_verify_then_pull_pulls_by_digest_then_retags(monkeypatch):
    _patch_provenance(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    out = recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")
    assert out == _D
    verbs = [c[0] for c in calls]
    # only the VERIFIED digest is pulled, and the retag happens AFTER the pull
    assert ["pull", f"greffon/greffer@{_D}"] in calls
    assert ["tag", f"greffon/greffer@{_D}", "greffon/greffer:latest"] in calls
    assert verbs.index("pull") < verbs.index("tag")


def test_verify_then_pull_unresolvable_digest_refuses(monkeypatch):
    monkeypatch.setattr(provenance, "resolve_digest", lambda ref: None)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no docker when digest unresolved"))
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


def test_verify_then_pull_cosign_failure_refuses_before_pull(monkeypatch):
    _patch_provenance(monkeypatch, cosign_ok=False)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no pull when cosign fails"))
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


def test_verify_then_pull_pull_failure_refuses_before_retag(monkeypatch):
    _patch_provenance(monkeypatch)

    def fake(args, *, timeout=None):
        if args[1] == "pull":
            return _fail()
        if args[1] == "tag":
            pytest.fail("retag reached after a failed pull")
        return _ok()
    monkeypatch.setattr(compose, "_run", fake)
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


def test_verify_then_pull_retag_failure_refuses(monkeypatch):
    _patch_provenance(monkeypatch)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: _fail() if a[1] == "tag" else _ok())
    with pytest.raises(recreate.VerifyError):
        recreate.verify_then_pull("greffon/greffer", cosign_pub="/k")


# --- :previous tag, image-id capture, dangling prune ----------------

def test_current_image_id_reads_latest(monkeypatch):
    monkeypatch.setattr(
        compose, "_run",
        lambda a, **k: _ok("sha256:deadbeef\n") if a[1:3] == ["image", "inspect"] else _ok())
    assert recreate.current_image_id("greffon/greffer") == "sha256:deadbeef"


def test_current_image_id_absent_is_none(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert recreate.current_image_id("greffon/greffer") is None


def test_tag_previous_tags_outgoing_image(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    assert recreate.tag_previous("greffon/greffer", "sha256:old") is True
    assert ["tag", "sha256:old", "greffon/greffer:previous"] in calls


def test_tag_previous_empty_id_is_noop(monkeypatch):
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no docker for an empty image id"))
    assert recreate.tag_previous("greffon/greffer", "") is False


def test_tag_previous_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _fail())
    assert recreate.tag_previous("greffon/greffer", "sha256:old") is False


def test_dangling_prune_is_dangling_only(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    recreate.dangling_prune()
    assert ["image", "prune", "-f"] in calls
    # never -a (that would reap the tagged :previous / unused images) and never rmi
    assert not any("-a" in c for c in calls)
    assert not any(c and c[0] == "rmi" for c in calls)


# --- fidelity recreate: build_create_argv (HLD section 8, vs-OLD delta) ----

def _greffer_inspect() -> dict:
    return {
        "Name": "/greffer-greffer-1",
        "Config": {
            "Env": [
                "PATH=/opt/venv/bin:/usr/bin",
                "VIRTUAL_ENV=/opt/venv",
                "GREFFER_UPDATER_IMAGE=greffon/greffer-updater@sha256:OLD",
                "GREFFER_ID=g1",
                "GREFFON_BASE_SERVER=https://api",
            ],
            "Labels": {
                "com.docker.compose.project": "greffer",
                "com.docker.compose.service": "greffer",
                "org.opencontainers.image.version": "0.3.4",
            },
            "Cmd": ["uvicorn"], "Entrypoint": None, "Healthcheck": None,
        },
        "HostConfig": {
            "NetworkMode": "greffer_internal",
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "PortBindings": {},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
            "ExtraHosts": ["host.docker.internal:host-gateway"],
        },
        "Mounts": [
            {"Type": "bind", "Source": "/var/run/docker.sock",
             "Destination": "/var/run/docker.sock", "RW": True},
            {"Type": "volume", "Name": "greffer_greffon-data",
             "Destination": "/data", "RW": True},
        ],
    }


def _old_greffer_image_config() -> dict:
    # The OLD image baked PATH/VIRTUAL_ENV and the OLD updater digest; no GREFFER_ID.
    return {
        "Env": [
            "PATH=/opt/venv/bin:/usr/bin",
            "VIRTUAL_ENV=/opt/venv",
            "GREFFER_UPDATER_IMAGE=greffon/greffer-updater@sha256:OLD",
        ],
        "Labels": {"org.opencontainers.image.version": "0.3.4"},
        "Cmd": ["uvicorn"], "Entrypoint": None, "Healthcheck": None,
    }


def test_build_argv_env_delta_drops_baked_keeps_runtime():
    argv = recreate.build_create_argv(
        _greffer_inspect(), _old_greffer_image_config(),
        image_ref="greffon/greffer:latest", service="greffer")
    # baked vars equal to the OLD image are NOT carried -> the NEW image wins
    # (this is the whole point of vs-OLD: a relocated venv or a new updater
    # digest in the new image must not be clobbered by the old value).
    assert not any(a.startswith("VIRTUAL_ENV=") for a in argv)
    assert not any(a.startswith("PATH=") for a in argv)
    assert not any(a.startswith("GREFFER_UPDATER_IMAGE=") for a in argv)
    # runtime (compose-set) env IS carried
    assert "GREFFER_ID=g1" in argv
    assert "GREFFON_BASE_SERVER=https://api" in argv


def test_build_argv_operator_pinned_updater_image_is_carried():
    cont = _greffer_inspect()
    cont["Config"]["Env"] = [
        e for e in cont["Config"]["Env"] if not e.startswith("GREFFER_UPDATER_IMAGE=")
    ] + ["GREFFER_UPDATER_IMAGE=greffon/greffer-updater@sha256:PIN"]
    argv = recreate.build_create_argv(
        cont, _old_greffer_image_config(),
        image_ref="greffon/greffer:latest", service="greffer")
    # an env.env pin differs from the old image's bake -> carried (pin wins)
    assert "GREFFER_UPDATER_IMAGE=greffon/greffer-updater@sha256:PIN" in argv


def test_build_argv_carries_compose_labels_not_image_label():
    argv = recreate.build_create_argv(
        _greffer_inspect(), _old_greffer_image_config(),
        image_ref="greffon/greffer:latest", service="greffer")
    assert "com.docker.compose.service=greffer" in argv
    assert "com.docker.compose.project=greffer" in argv
    # the version label matches the old image -> not carried (new image supplies it)
    assert not any(a.startswith("org.opencontainers.image.version=") for a in argv)


def test_build_argv_greffer_infra_fields_and_image_last():
    argv = recreate.build_create_argv(
        _greffer_inspect(), _old_greffer_image_config(),
        image_ref="greffon/greffer:latest", service="greffer")
    i = argv.index("--network")
    assert argv[i + 1] == "greffer_internal"
    assert "--network-alias" in argv and "greffer" in argv
    assert "--restart" in argv and "unless-stopped" in argv
    assert "--name" in argv and "greffer-greffer-1" in argv
    assert "/var/run/docker.sock:/var/run/docker.sock" in argv  # bind mount
    assert "greffer_greffon-data:/data" in argv                 # named volume
    assert "host.docker.internal:host-gateway" in argv          # extra host
    assert "--log-opt" in argv and "max-size=10m" in argv
    # cmd override is empty (matches old image) -> the image ref is last
    assert argv[-1] == "greffon/greffer:latest"


def test_build_argv_sidecar_host_network_ro_mount_healthcheck():
    sidecar = {
        "Name": "/greffer-tunnel-sidecar-1",
        "Config": {
            "Env": ["PATH=/usr/bin"],
            "Labels": {"com.docker.compose.service": "tunnel-sidecar",
                       "com.docker.compose.project": "greffer"},
            "Cmd": None, "Entrypoint": None,
            "Healthcheck": {"Test": ["CMD-SHELL", "pgrep rathole"],
                            "Interval": 30000000000, "Timeout": 5000000000, "Retries": 3},
        },
        "HostConfig": {
            "NetworkMode": "host",
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {}, "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "Mounts": [{"Type": "volume", "Name": "greffer_rathole-client-config",
                    "Destination": "/config", "RW": False}],
    }
    old_img = {"Env": ["PATH=/usr/bin"], "Labels": {}, "Healthcheck": None}
    argv = recreate.build_create_argv(
        sidecar, old_img, image_ref="greffon/tunnel-sidecar:latest",
        service="tunnel-sidecar")
    j = argv.index("--network")
    assert argv[j + 1] == "host"
    assert "--network-alias" not in argv          # host network takes no alias
    assert "greffer_rathole-client-config:/config:ro" in argv  # :ro is load-bearing
    assert "--health-cmd" in argv and "pgrep rathole" in argv  # compose-set healthcheck
    assert "--health-interval" in argv and "30s" in argv
    assert "--health-retries" in argv and "3" in argv


def test_build_argv_nginx_port_binding():
    nginx = {
        "Name": "/greffer-nginx-1",
        "Config": {"Env": [], "Labels": {"com.docker.compose.service": "nginx"},
                   "Cmd": None, "Entrypoint": None, "Healthcheck": None},
        "HostConfig": {
            "NetworkMode": "greffer_internal",
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {"443/tcp": [{"HostIp": "", "HostPort": "8001"}]},
            "LogConfig": {"Type": "json-file", "Config": {}},
        },
        "Mounts": [],
    }
    argv = recreate.build_create_argv(
        nginx, {"Env": [], "Labels": {}}, image_ref="greffon/greffer-nginx:latest",
        service="nginx")
    assert "-p" in argv and "8001:443" in argv


def test_build_argv_unreadable_old_image_carries_env_verbatim():
    # fail-open: empty old-image config -> everything differs -> carried (we would
    # rather over-carry runtime env than drop GREFFER_ID; logged by recreate_one).
    argv = recreate.build_create_argv(
        _greffer_inspect(), {}, image_ref="greffon/greffer:latest", service="greffer")
    assert "GREFFER_ID=g1" in argv
    assert any(a.startswith("VIRTUAL_ENV=") for a in argv)


# --- recreate_one (inspect -> stop -> rm -> create -> start) ---------

def test_recreate_one_happy_orders_calls(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(recreate, "inspect_container", lambda cid: _greffer_inspect())
    monkeypatch.setattr(recreate, "image_config", lambda ref: _old_greffer_image_config())
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    c = recreate.StackContainer("greffer", "cid1", "greffon/greffer")
    assert recreate.recreate_one(
        c, image_ref="greffon/greffer:latest", old_image_id="sha256:old") is True
    assert [x[0] for x in calls] == ["stop", "rm", "create", "start"]
    create = next(x for x in calls if x[0] == "create")
    assert create[-1] == "greffon/greffer:latest"  # creates from the new image


def test_recreate_one_inspect_failure_does_not_touch_docker(monkeypatch):
    monkeypatch.setattr(recreate, "inspect_container", lambda cid: None)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: pytest.fail("no docker mutation when inspect fails"))
    c = recreate.StackContainer("greffer", "cid1", "greffon/greffer")
    assert recreate.recreate_one(
        c, image_ref="x:latest", old_image_id="sha256:old") is False


def test_recreate_one_rm_failure_aborts_before_create(monkeypatch):
    monkeypatch.setattr(recreate, "inspect_container", lambda cid: _greffer_inspect())
    monkeypatch.setattr(recreate, "image_config", lambda ref: {})

    def fake(a, **k):
        if a[1] == "rm":
            return _fail()
        if a[1] in ("create", "start"):
            pytest.fail("reached create/start after a failed rm")
        return _ok()
    monkeypatch.setattr(compose, "_run", fake)
    c = recreate.StackContainer("greffer", "cid1", "greffon/greffer")
    assert recreate.recreate_one(
        c, image_ref="x:latest", old_image_id="sha256:old") is False


def test_recreate_one_create_failure_aborts_before_start(monkeypatch):
    monkeypatch.setattr(recreate, "inspect_container", lambda cid: _greffer_inspect())
    monkeypatch.setattr(recreate, "image_config", lambda ref: {})

    def fake(a, **k):
        if a[1] == "create":
            return _fail()
        if a[1] == "start":
            pytest.fail("reached start after a failed create")
        return _ok()
    monkeypatch.setattr(compose, "_run", fake)
    c = recreate.StackContainer("greffer", "cid1", "greffon/greffer")
    assert recreate.recreate_one(
        c, image_ref="x:latest", old_image_id="sha256:old") is False


def test_recreate_one_start_failure_is_reported(monkeypatch):
    monkeypatch.setattr(recreate, "inspect_container", lambda cid: _greffer_inspect())
    monkeypatch.setattr(recreate, "image_config", lambda ref: {})
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: _fail() if a[1] == "start" else _ok())
    c = recreate.StackContainer("greffer", "cid1", "greffon/greffer")
    assert recreate.recreate_one(
        c, image_ref="x:latest", old_image_id="sha256:old") is False


def test_recreate_one_uses_passed_inspect_without_reinspecting(monkeypatch):
    monkeypatch.setattr(recreate, "inspect_container",
                        lambda cid: pytest.fail("should not re-inspect when inspect is passed"))
    monkeypatch.setattr(recreate, "image_config", lambda ref: {})
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok())
    c = recreate.StackContainer("greffer", "cid1", "greffon/greffer")
    assert recreate.recreate_one(
        c, image_ref="greffon/greffer:latest", old_image_id="sha256:old",
        inspected=_greffer_inspect()) is True


# --- verify_and_pull / retag_latest split (fail-closed before any tag moves) ---

def test_verify_and_pull_does_not_retag(monkeypatch):
    _patch_provenance(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    assert recreate.verify_and_pull("greffon/greffer", cosign_pub="/k") == _D
    assert ["pull", f"greffon/greffer@{_D}"] in calls
    assert not any(c and c[0] == "tag" for c in calls)  # no :latest move


def test_verify_and_pull_pull_failure_raises(monkeypatch):
    _patch_provenance(monkeypatch)
    monkeypatch.setattr(compose, "_run",
                        lambda a, **k: _fail() if a[1] == "pull" else _ok())
    with pytest.raises(recreate.VerifyError):
        recreate.verify_and_pull("greffon/greffer", cosign_pub="/k")


def test_retag_latest_points_latest_at_digest(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    assert recreate.retag_latest("greffon/greffer", _D) is True
    assert ["tag", f"greffon/greffer@{_D}", "greffon/greffer:latest"] in calls


# --- rollback_one (recreate from the OLD image id, by NAME) ----------

def test_rollback_one_recreates_from_old_image_by_name(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(recreate, "image_config", lambda ref: {})
    monkeypatch.setattr(compose, "_run", lambda a, **k: (calls.append(a[1:]), _ok())[1])
    c = recreate.StackContainer("greffer", "new-cid-after-recreate", "greffon/greffer")
    inspected = {"Name": "/greffer-greffer-1", "Config": {}, "HostConfig": {}, "Mounts": []}
    assert recreate.rollback_one(c, inspected, "sha256:oldimg") is True
    assert [x[0] for x in calls] == ["stop", "rm", "create", "start"]
    create = next(x for x in calls if x[0] == "create")
    assert create[-1] == "sha256:oldimg"            # recreated FROM the old image
    # targets by NAME (the forward recreate changed the id), not the stale cid
    assert ["stop", "-t", "30", "greffer-greffer-1"] == calls[0]
    assert "new-cid-after-recreate" not in [tok for c2 in calls for tok in c2]


# --- socket probes for the gate -------------------------------------

def test_exec_readyz_uses_shared_probe(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(compose, "_run", lambda a, **k: (seen.update(args=a), _ok("{}"))[1])
    recreate.exec_readyz("greffer-greffer-1")
    assert seen["args"][:4] == ["docker", "exec", "greffer-greffer-1", "python"]
    assert compose.GREFFER_READYZ_PROBE in seen["args"]  # shared, cannot drift


def test_exec_version_reads_stdout(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("0.3.5\n"))
    assert recreate.exec_version("greffer-greffer-1") == "0.3.5"


def test_container_running(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("true\n"))
    assert recreate.container_running("x") is True
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("false\n"))
    assert recreate.container_running("x") is False


def test_container_image_id_by_name(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("sha256:img\n"))
    assert recreate.container_image_id_by_name("x") == "sha256:img"


def test_restart_count_parses_int(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("3\n"))
    assert recreate.restart_count("x") == 3


def test_restart_count_unparseable_is_zero(monkeypatch):
    monkeypatch.setattr(compose, "_run", lambda a, **k: _ok("nope"))
    assert recreate.restart_count("x") == 0
