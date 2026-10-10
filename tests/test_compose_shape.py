"""Tests for compose-shape normalization (compose-containment Feature 2):
every legal spelling of ports/volumes/networks normalizes to one shape, and
every construct the pipeline cannot honour refuses with a ComposeShapeError
naming the service and key — never an uncaught exception (a bare 500).

The epic's seven-construct failure table is the spec: each row gets a
support case (normalizes; extraction identical to the legacy string-split
for the shapes that parsed before) and/or a refusal case asserting the
STATUS-equivalent error names the offending service."""

from __future__ import annotations

import pytest

from apps.utils.greffon import repository
from apps.utils.greffon.compose_shape import ComposeShapeError, normalize_compose


def _compose(service=None):
    service = service if service is not None else {}
    return {"services": {"app": service}}


def _normalized(service=None):
    return normalize_compose(_compose(service))["services"]["app"]


class TestPortsNormalize:
    def test_bare_string(self):
        assert _normalized({"ports": ["8080"]})["ports"] == [
            {"target": "8080", "protocol": None}
        ]

    def test_bare_int(self):
        # Epic table row: '8080' as int raised AttributeError on .split.
        assert _normalized({"ports": [8080]})["ports"] == [
            {"target": "8080", "protocol": None}
        ]

    def test_host_container(self):
        assert _normalized({"ports": ["80:80"]})["ports"] == [
            {"target": "80", "protocol": None}
        ]

    def test_ip_host_container_udp(self):
        assert _normalized({"ports": ["127.0.0.1:8080:80/udp"]})["ports"] == [
            {"target": "80", "protocol": "udp"}
        ]

    def test_long_syntax(self):
        # Epic table row: the {target, published} mapping raised
        # AttributeError on .split.
        entry = {"target": 80, "published": 8080, "protocol": "udp"}
        assert _normalized({"ports": [entry]})["ports"] == [
            {"target": "80", "protocol": "udp"}
        ]

    def test_uppercase_protocol_lowered(self):
        assert _normalized({"ports": ["53/UDP"]})["ports"][0]["protocol"] == "udp"

    def test_idempotent(self):
        once = _normalized({"ports": ["80:80"]})
        twice = normalize_compose({"services": {"app": {"ports": once["ports"]}}})
        assert twice["services"]["app"]["ports"] == once["ports"]


class TestPortsRefuse:
    def test_non_numeric_target(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"ports": ["http"]})
        assert "services.app.ports" in str(e.value)

    def test_long_syntax_missing_target(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"ports": [{"published": 8080}]})
        assert "target" in str(e.value)

    def test_unknown_protocol(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"ports": ["53/sctp"]})
        assert "protocol" in str(e.value)

    def test_mapping_form_refused(self):
        # The legacy dead else-branch shape: invalid compose, now refused
        # instead of referencing an unbound local.
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"ports": {"80": "80"}})
        assert isinstance(e.value.value, dict)


class TestVolumesNormalize:
    def test_two_part(self):
        assert _normalized({"volumes": ["db_data:/var/lib/postgresql"]})["volumes"] == [
            {"source": "db_data", "target": "/var/lib/postgresql", "mode": None}
        ]

    def test_three_part_ro(self):
        # Epic table row: 'vol:/path:ro' raised ValueError on unpack. The
        # mode is PRESERVED (a dropped :ro would deploy read-only mounts
        # read-write).
        assert _normalized({"volumes": ["db_data:/data:ro"]})["volumes"] == [
            {"source": "db_data", "target": "/data", "mode": "ro"}
        ]

    def test_long_syntax(self):
        # Epic table row: {type, source, target} raised AttributeError.
        entry = {"type": "volume", "source": "db_data", "target": "/data"}
        assert _normalized({"volumes": [entry]})["volumes"] == [
            {"source": "db_data", "target": "/data", "mode": None}
        ]

    def test_long_syntax_mode_honoured(self):
        entry = {"type": "volume", "source": "db", "target": "/d", "mode": "ro"}
        assert _normalized({"volumes": [entry]})["volumes"][0]["mode"] == "ro"

    def test_long_syntax_read_only_renders_ro(self):
        # Codex review: long syntax expresses access with read_only, not the
        # short syntax's mode token — ignoring it deployed a declared
        # read-only mount WRITABLE. The rendered mapping must stay :ro.
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = {"services": {"app": {"image": "x", "volumes": [
            {"type": "volume", "source": "db", "target": "/d",
             "read_only": True}]}}}
        info = repository.create_greffon_info(compose, _greffon())
        rendered = create_compose_template_from_greffon(compose, info)
        mounts = rendered["services"]["app"]["volumes"]
        assert any(m.endswith(":ro") for m in mounts), mounts

    def test_long_syntax_nested_options_refused(self):
        # subpath/nocopy cannot be expressed in the rebuilt short syntax;
        # dropping them would deploy a DIFFERENT mount, so refuse.
        with pytest.raises(ComposeShapeError, match="subpath/nocopy"):
            _normalized({"volumes": [
                {"type": "volume", "source": "db", "target": "/d",
                 "volume": {"subpath": "tenant1"}}]})
        with pytest.raises(ComposeShapeError, match="subpath/nocopy"):
            _normalized({"volumes": [
                {"type": "volume", "source": "db", "target": "/d",
                 "volume": {"nocopy": True}}]})

    def test_mode_preserved_into_render(self):
        # The end-to-end bar: a read-only declaration must stay read-only
        # through the rebuild (the mapping is re-rendered, not copied).
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = {
            "services": {"app": {"image": "x", "volumes": ["db_data:/data:ro"]}}
        }
        info = repository.create_greffon_info(compose, _greffon())
        rendered = create_compose_template_from_greffon(compose, info)
        mounts = rendered["services"]["app"]["volumes"]
        assert any(m.endswith(":ro") for m in mounts), mounts

    def test_modeless_mount_renders_without_suffix(self):
        from apps.utils.docker.compose import create_compose_template_from_greffon

        compose = {"services": {"app": {"image": "x", "volumes": ["db:/data"]}}}
        info = repository.create_greffon_info(compose, _greffon())
        rendered = create_compose_template_from_greffon(compose, info)
        mounts = rendered["services"]["app"]["volumes"]
        assert all(not m.rsplit(":", 1)[-1] in ("ro", "rw") for m in mounts), mounts


class TestVolumesRefuse:
    def test_unknown_mode(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"volumes": ["db_data:/data:banna"]})
        assert "mount mode" in str(e.value)

    def test_four_part(self):
        with pytest.raises(ComposeShapeError):
            _normalized({"volumes": ["a:b:c:d"]})

    def test_bind_short(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"volumes": ["/host/path:/data"]})
        assert "bind mounts are structurally unsupported" in str(e.value)

    def test_bind_long(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized(
                {"volumes": [{"type": "bind", "source": "/host", "target": "/data"}]}
            )
        assert "bind" in str(e.value)

    def test_tmpfs_long(self):
        with pytest.raises(ComposeShapeError):
            _normalized({"volumes": [{"type": "tmpfs", "target": "/tmp"}]})

    def test_anonymous(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"volumes": ["/data"]})
        assert "anonymous" in str(e.value)

    def test_relative_target(self):
        with pytest.raises(ComposeShapeError):
            _normalized({"volumes": ["db_data:data"]})


class TestNetworks:
    def test_service_list_form(self):
        compose = {
            "networks": {"backend": {}},
            "services": {"app": {"networks": ["backend"]}},
        }
        normalize_compose(compose)
        assert compose["services"]["app"]["networks"] == ["backend"]

    def test_service_dict_form(self):
        # Epic table row: mapping form raised TypeError unhashable 'dict'.
        compose = {
            "networks": {"backend": {}},
            "services": {"app": {"networks": {"backend": {"aliases": ["app-1"]}}}},
        }
        normalize_compose(compose)
        assert compose["services"]["app"]["networks"] == ["backend"]

    def test_top_level_list_form(self):
        compose = {
            "networks": ["backend"],
            "services": {"app": {"networks": ["backend"]}},
        }
        normalize_compose(compose)
        assert compose["services"]["app"]["networks"] == ["backend"]

    def test_implicit_default_end_to_end(self):
        # compose's implicit 'default' network is referenceable without a
        # top-level declaration. The normalizer allows it, so the CONSUMER
        # must tolerate it: create_greffon_info must not KeyError on the
        # unregistered name. (The vacuous normalize-only version of this
        # test once passed while the route 500'd — adversarial-review
        # finding.) 'default' is a membership no-op: the service rides the
        # internal network like every other, and nothing named 'default'
        # is invented.
        compose = {"services": {"app": {"networks": ["default"]}}}
        info = repository.create_greffon_info(compose, _greffon())
        assert "default" not in info["networks"]
        assert (
            "app" in info["networks"]["greffon_internal_network"]["containers"]
        )

    def test_implicit_default_string_form_end_to_end(self):
        compose = {"services": {"app": {"networks": "default"}}}
        info = repository.create_greffon_info(compose, _greffon())
        assert "default" not in info["networks"]

    def test_default_alongside_declared_network_end_to_end(self):
        compose = {
            "networks": {"backend": {}},
            "services": {"app": {"networks": ["default", "backend"]}},
        }
        info = repository.create_greffon_info(compose, _greffon())
        assert info["networks"]["backend"]["containers"] == ["app"]
        assert "default" not in info["networks"]

    def test_empty_networks_list_is_noop(self):
        # networks: [] is legal compose (opt out of the default network) and
        # the pipeline still places the service on the internal network, so
        # it normalizes as a no-op instead of refusing (a refusal would
        # regress a spelling the legacy pipeline deployed fine).
        assert _normalized({"networks": []})["networks"] == []
        compose = {"services": {"app": {"networks": []}}}
        info = repository.create_greffon_info(compose, _greffon())
        assert (
            "app" in info["networks"]["greffon_internal_network"]["containers"]
        )

    def test_port_range_message(self):
        with pytest.raises(ComposeShapeError) as e:
            _normalized({"ports": ["3000-3005:3000-3005"]})
        assert "port ranges are not supported" in str(e.value)

    def test_undeclared_refused(self):
        # Previously a KeyError (500); now a named refusal.
        compose = {"services": {"app": {"networks": ["ghost"]}}}
        with pytest.raises(ComposeShapeError) as e:
            normalize_compose(compose)
        assert "ghost" in str(e.value)
        assert "services.app.networks" in str(e.value)

    def test_end_to_end_greffon_info(self):
        # Epic table row: a top-level networks block used by a service raised
        # AttributeError ('dict' has no attribute 'append'). The full
        # create_greffon_info path now lands the service on the network with
        # containers as a LIST (the shape the rebuild iterates).
        compose = {
            "networks": {"backend": {}},
            "services": {
                "app": {"image": "x", "networks": ["backend"]},
                "db": {"image": "y"},
            },
        }
        info = repository.create_greffon_info(compose, _greffon())
        assert info["networks"]["backend"]["containers"] == ["app"]


class TestTopLevel:
    def test_missing_services(self):
        # Epic table row: KeyError; now a named refusal.
        with pytest.raises(ComposeShapeError) as e:
            normalize_compose({"version": "3"})
        assert e.value.key == "services"
        assert "services" in str(e.value)

    def test_non_mapping_compose(self):
        with pytest.raises(ComposeShapeError):
            normalize_compose(["services"])

    def test_service_not_mapping(self):
        with pytest.raises(ComposeShapeError) as e:
            normalize_compose({"services": {"app": "nginx"}})
        assert "services.app" in str(e.value)


def _greffon():
    return {
        "id": "i1",
        "configurations": [],
        "ports": {},
        "cert": {"certificate": "", "private_key": ""},
    }


# The catalog regression bar, at unit scope: for the short forms the catalog
# uses, create_greffon_info derives identical port/volume values through the
# normalizer as the legacy string-split did.
_EQUIVALENCE_CASES = [
    (
        {"ports": ["80:80"], "volumes": ["db:/data"]},
        ("app_80", "tcp", "db", "/data"),
    ),
    ({"ports": [8080]}, ("app_8080", "tcp", None, None)),
    ({"ports": ["53/udp"]}, ("app_53", "udp", None, None)),
    ({"ports": ["127.0.0.1:9000:9000"]}, ("app_9000", "tcp", None, None)),
]


class TestExtractionEquivalence:
    @pytest.mark.parametrize("service,expected", _EQUIVALENCE_CASES)
    def test_extraction(self, service, expected):
        port_name, proto, vol, target = expected
        info = repository.create_greffon_info(_compose(service), _greffon())
        port = info["ports"][0]
        assert (port["port_name"], port["protocol"]) == (port_name, proto)
        if vol is not None:
            assert info["volumes"][vol]["containers"] == {"app": {"path": target}}
        else:
            assert set(info["volumes"]) == {"greffon_nginx"}
