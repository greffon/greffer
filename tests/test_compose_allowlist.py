"""Tests for the compose key allowlist, mount namespacing, and baked-file
staging (compose-containment Feature 3).

The allowlist is trust-scoped because a flat list breaks the catalog:
wireguard/1.0 legitimately uses cap_add/cap_drop/devices/sysctls. The
operator grant is the machine owner's per-node call. env_file is refused in
BOTH sets (client-side path traversal, the bind-mount reach by another
door). The catalog gate validates the catalog set against every pinned
catalog entry — the HLD's merge condition, so a too-narrow list cannot take
shipped apps offline."""

from __future__ import annotations

import os

import pytest

from apps.utils.greffon import repository
from apps.utils.greffon.compose_shape import (
    ComposeShapeError,
    normalize_compose,
    _parse_extra_keys,
)


def _svc(key, value):
    return {"services": {"app": {"image": "x", key: value}}}


class TestKeyAllowlist:
    def test_catalog_set_allows_privileged_keys(self):
        # wireguard's exact privileged surface passes the catalog set.
        compose = {
            "services": {
                "wg": {
                    "image": "x",
                    "cap_add": ["NET_ADMIN", "NET_RAW"],
                    "cap_drop": ["ALL"],
                    "devices": ["/dev/net/tun:/dev/net/tun"],
                    "sysctls": ["net.ipv4.ip_forward=1"],
                    "shm_size": "128mb",
                }
            }
        }
        normalize_compose(compose)  # no raise

    def test_strict_refuses_privileged_keys(self):
        with pytest.raises(ComposeShapeError) as e:
            normalize_compose(
                _svc("devices", ["/dev/net/tun:/dev/net/tun"]), strict=True
            )
        assert "services.app.devices" in str(e.value)
        assert "strict" in str(e.value)

    def test_strict_refuses_cap_add_and_sysctls(self):
        for key, val in (("cap_add", ["NET_ADMIN"]), ("sysctls", ["a=1"])):
            with pytest.raises(ComposeShapeError, match=key):
                normalize_compose(_svc(key, val), strict=True)

    def test_common_keys_pass_strict(self):
        compose = {
            "services": {
                "app": {
                    "image": "x",
                    "command": ["run"],
                    "restart": "unless-stopped",
                    "environment": {"A": "b"},
                    "shm_size": "64mb",
                }
            }
        }
        normalize_compose(compose, strict=True)  # no raise

    def test_unknown_service_key_refused_catalog(self):
        # devices' cousins the epic named: nothing enumerates them, the
        # allowlist simply does not contain them.
        for key in (
            "privileged",
            "volumes_from",
            "build",
            "ipc",
            "userns_mode",
            "pid",
            "network_mode",
            "cgroup_parent",
        ):
            with pytest.raises(ComposeShapeError, match=key):
                normalize_compose(_svc(key, "host" if key != "build" else "."))

    def test_env_file_refused_in_both_sets(self):
        with pytest.raises(ComposeShapeError, match="env_file"):
            normalize_compose(_svc("env_file", "../other/realm.json"))
        with pytest.raises(ComposeShapeError, match="env_file"):
            normalize_compose(_svc("env_file", ".env"), strict=True)

    def test_unknown_top_level_refused(self):
        for key in ("secrets", "configs", "include", "extends"):
            with pytest.raises(ComposeShapeError) as e:
                normalize_compose({"services": {"app": {"image": "x"}}, key: {}})
            assert e.value.service is None
            assert key in str(e.value)

    def test_managed_keys_pass_both_sets(self):
        # ports/volumes/networks are greffer-managed; the shape pass owns
        # their validation, not the allowlist.
        compose = {
            "services": {
                "app": {"image": "x", "ports": ["80:80"], "volumes": ["d:/d"]}
            },
            "volumes": {"d": {}},
            "networks": {},
            "version": "3",
        }
        for strict in (False, True):
            import copy

            normalize_compose(copy.deepcopy(compose), strict=strict)  # no raise

    def test_operator_grant_adds_key(self):
        compose = _svc("cap_add", ["NET_ADMIN"])
        with pytest.raises(ComposeShapeError):
            normalize_compose(compose, strict=True)
        normalize_compose(
            _svc("cap_add", ["NET_ADMIN"]), strict=True, extra_keys="cap_add"
        )  # no raise

    def test_logging_key_passes_both_sets_and_injection_honors_it(self):
        # _inject_instance_log_rotation documents "a catalog author's
        # explicit choice wins"; the allowlist must keep that reachable.
        from apps.utils.docker.compose import _inject_instance_log_rotation

        for strict in (False, True):
            compose = {"services": {"app": {
                "image": "x",
                "logging": {"driver": "syslog",
                             "options": {"tag": "app"}},
            }}}
            normalize_compose(compose, strict=strict)  # no raise
            _inject_instance_log_rotation(compose)
            # the author's block is preserved, not overwritten
            assert compose["services"]["app"]["logging"]["driver"] == "syslog"

    def test_env_file_never_grantable(self):
        # The operator override cannot re-admit env_file: it re-opens the
        # cross-instance baked-secret read, a hole that harms tenants.
        with pytest.raises(ComposeShapeError, match="env_file"):
            normalize_compose(
                _svc("env_file", ".env"), strict=True, extra_keys="env_file"
            )

    def test_extra_keys_parsing(self):
        assert _parse_extra_keys("") == frozenset()
        assert _parse_extra_keys("a, b ,c") == {"a", "b", "c"}
        assert _parse_extra_keys(None) == frozenset()


class TestMountNamespacing:
    def _greffon(self, iid="i1"):
        return {
            "id": iid,
            "configurations": [],
            "ports": {},
            "cert": {"certificate": "", "private_key": ""},
        }

    def test_undeclared_source_is_namespaced(self):
        # The legacy behaviour passed undeclared sources through verbatim, so
        # two instances mounting 'dd:/data' shared one host volume.
        compose = {"services": {"app": {"image": "x", "volumes": ["dd:/data"]}}}
        info = repository.create_greffon_info(compose, self._greffon("i9"))
        assert info["volumes"]["dd"]["value"] == "i9_dd"

    def test_declared_source_still_namespaced(self):
        compose = {
            "services": {"app": {"image": "x", "volumes": ["db:/data"]}},
            "volumes": {"db": {}},
        }
        info = repository.create_greffon_info(compose, self._greffon("i1"))
        assert info["volumes"]["db"]["value"] == "i1_db"

    def test_two_instances_get_distinct_volume_values(self):
        c1 = {"services": {"app": {"image": "x", "volumes": ["dd:/data"]}}}
        c2 = {"services": {"app": {"image": "x", "volumes": ["dd:/data"]}}}
        v1 = repository.create_greffon_info(c1, self._greffon("i1"))["volumes"]["dd"][
            "value"
        ]
        v2 = repository.create_greffon_info(c2, self._greffon("i2"))["volumes"]["dd"][
            "value"
        ]
        assert v1 != v2

    def test_rendered_compose_declares_namespaced_volume(self):
        # The rebuilt top-level volumes block must carry the namespaced name
        # the service mapping references, or docker-compose rejects the file.
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = {"services": {"app": {"image": "x", "volumes": ["dd:/data"]}}}
        info = repository.create_greffon_info(compose, self._greffon("i1"))
        rendered = create_compose_template_from_greffon(compose, info)
        assert "i1_dd" in rendered["volumes"]
        assert any(
            m.startswith("i1_dd:") for m in rendered["services"]["app"]["volumes"]
        )


class TestBakedFileStaging:
    def _info(self, monkeypatch, tmpdir, configurations=None):
        # monkeypatch (not raw os.environ): GREFFON_PATH leaking into later
        # test files breaks the l4 sticky-port tests, which resolve their
        # state files against it.
        monkeypatch.setenv("GREFFON_PATH", str(tmpdir))
        info = {
            "id": "i1",
            "ports": [],
            "configurations": configurations or [],
            "volumes": {
                "app_conf": {
                    "name": "app_conf",
                    "value": "i1_app_conf",
                    "containers": {},
                    "files": [],
                },
            },
        }
        return info

    def _dest(self, name, volume="app_conf", kind="file", value=None):
        return {
            "type": kind,
            "name": name,
            "volume": volume,
            "value": value if value is not None else {"value": "x"},
        }

    def test_stages_under_config_dir(self, tmp_path, monkeypatch):
        from apps.utils.docker import compose as C

        info = self._info(
            monkeypatch,
            tmp_path,
            [
                {
                    "value": {
                        "file": ("data:application/octet-stream;base64,aGVsbG8=")
                    },
                    "destinations": [self._dest("realm.json")],
                }
            ],
        )
        C.apply_configuration(info, {})
        staged = os.path.join(str(tmp_path), "i1", "config", "realm.json")
        assert os.path.isfile(staged)
        assert info["volumes"]["app_conf"]["files"][-1]["src"] == staged

    def test_traversal_name_refused(self, tmp_path, monkeypatch):
        from apps.utils.docker import compose as C

        for bad in (
            "../escape.json",
            "/abs/path.json",
            "a/b.json",
            "..",
            "a\\b.json",
            ".",
        ):
            info = self._info(
                monkeypatch,
                tmp_path,
                [
                    {
                        "value": {
                            "file": ("data:application/octet-stream;base64,aGVsbG8=")
                        },
                        "destinations": [self._dest(bad)],
                    }
                ],
            )
            with pytest.raises(C.ConfigRenderError, match="bare file name"):
                C.apply_configuration(info, {})
        # Nothing written anywhere under the instance dir: every case was
        # refused before the staging dir was even created.
        written = [
            os.path.join(d, f)
            for d, _dirs, files in os.walk(os.path.join(str(tmp_path), "i1"))
            for f in files
        ]
        assert written == []

    def test_control_char_name_refused(self, tmp_path, monkeypatch):
        # A NUL byte in the name passes the bare-name checks but open()
        # raises ValueError (a 500, not a clean refusal).
        from apps.utils.docker import compose as C

        info = self._info(
            monkeypatch,
            tmp_path,
            [
                {
                    "value": {
                        "file": ("data:application/octet-stream;base64,aGVsbG8=")
                    },
                    "destinations": [self._dest("a\x00b.json")],
                }
            ],
        )
        with pytest.raises(C.ConfigRenderError, match="bare file name"):
            C.apply_configuration(info, {})

    def test_greffon_nginx_volume_refused(self, tmp_path, monkeypatch):
        from apps.utils.docker import compose as C

        info = self._info(
            monkeypatch,
            tmp_path,
            [
                {
                    "value": {
                        "file": ("data:application/octet-stream;base64,aGVsbG8=")
                    },
                    "destinations": [self._dest("pem.crt", volume="greffon_nginx")],
                }
            ],
        )
        with pytest.raises(C.ConfigRenderError, match="certificate"):
            C.apply_configuration(info, {})

    def test_undeclared_volume_refused(self, tmp_path, monkeypatch):
        from apps.utils.docker import compose as C

        info = self._info(
            monkeypatch,
            tmp_path,
            [
                {
                    "value": {
                        "file": ("data:application/octet-stream;base64,aGVsbG8=")
                    },
                    "destinations": [self._dest("x.json", volume="ghost_vol")],
                }
            ],
        )
        with pytest.raises(C.ConfigRenderError, match="not declared"):
            C.apply_configuration(info, {})

    def test_json_destination_staged_and_validated(self, tmp_path, monkeypatch):
        from apps.utils.docker import compose as C

        info = self._info(
            monkeypatch,
            tmp_path,
            [
                {
                    "value": {"value": {"a": 1}},
                    "destinations": [self._dest("settings.json", kind="json")],
                }
            ],
        )
        C.apply_configuration(info, {})
        assert os.path.isfile(
            os.path.join(str(tmp_path), "i1", "config", "settings.json")
        )


def _find_catalog_root():
    """Locate the greffon-catalog checkout: GREFFON_CATALOG_DIR first (CI
    checks the catalog out to .greffon-catalog and exports the path — the
    ancestor search cannot find a dot-directory at a different name), then
    an ancestor search for a sibling checkout (local worktrees)."""
    env_dir = os.getenv("GREFFON_CATALOG_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        for name in ("greffon-catalog",):
            cand = os.path.join(d, name)
            if os.path.isdir(cand):
                return cand
        d = os.path.dirname(d)
    return None


class TestCatalogAllowlistGate:
    """The HLD's merge condition: the catalog allowlist must accept every
    entry in the pinned catalog checkout, or the default-closed list takes
    shipped apps offline on their next deploy. Fails (not skips) when the
    catalog is absent, with GREFFON_CATALOG_OPTIONAL=1 the deliberate
    opt-out — a gate that can silently cover nothing is not a gate."""

    def test_every_catalog_entry_passes_catalog_set(self):
        import yaml

        root = _find_catalog_root()
        if root is None:
            # Fail, never silently skip: a gate that can cover nothing is
            # not a gate. GREFFON_CATALOG_OPTIONAL=1 is the DELIBERATE,
            # visible opt-out (the render oracle's convention); CI checks
            # out the catalog, so absence is a misconfiguration worth
            # failing on.
            if os.getenv("GREFFON_CATALOG_OPTIONAL") == "1":
                pytest.skip("deliberate opt-out: GREFFON_CATALOG_OPTIONAL=1")
            pytest.fail(
                "greffon-catalog checkout not found; the allowlist "
                "gate must run against real entries (CI checks it out; "
                "set GREFFON_CATALOG_OPTIONAL=1 to skip deliberately)"
            )
        checked = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            if "docker-compose.yml" not in filenames:
                continue
            # only version dirs that look like catalog entries
            rel = os.path.relpath(dirpath, root)
            if rel.startswith((".git", "_template")) or os.sep not in rel:
                continue
            with open(os.path.join(dirpath, "docker-compose.yml")) as f:
                compose = yaml.safe_load(f)
            # The catalog set, no operator grant.
            normalize_compose(compose)
            checked += 1
        assert checked >= 20, (
            f"only {checked} catalog entries checked — gate covered too little"
        )
