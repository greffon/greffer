"""Tests for the deploy-time preflight (compose-containment Feature 6;
wire contract per custom-app LLD §4.1)."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from apps.utils.greffon import preflight as pf
from apps.utils.greffon.preflight import PreflightError, run_preflight

from app.auth import TOKEN_HEADER

_COMPOSE = """\
services:
  app:
    image: ghcr.io/me/app:1.2.3
    environment:
      DB_PASSWORD: "{{ config.DB_PASSWORD }}"
      MAIL_URL: "smtps://u:{{ config.MAIL_PW }}@smtp.gmail.com"
    ports:
      - "80:80"
"""

_DIGIST_OK = {"ghcr.io/me/app@sha256:" + "a" * 64}


def _mock_images_ok():
    images = MagicMock()
    info = MagicMock()
    info.attrs = {"RepoDigests": sorted(_DIGIST_OK)}
    images.get.return_value = info
    return images


class TestPreflightPayload:
    def test_ok(self):
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            result = run_preflight(_COMPOSE)
        assert result["services"] == ["app"]
        assert result["ports"] == [{"port_name": "app_80", "service": "app",
                                    "container_port": "80",
                                    "protocol": "tcp"}]
        assert result["image_digests"]["app"].startswith(
            "ghcr.io/me/app@sha256:")
        # candidates: DB_PASSWORD and MAIL_PW referenced pre-ticked
        by_key = {c["key"]: c for c in result["config_candidates"]}
        assert by_key["DB_PASSWORD"]["referenced"] is True
        assert by_key["MAIL_URL"]["referenced"] is False
        # the smtp.gmail.com literal warns (the strip's false-positive class)
        assert any(w["key"] == "MAIL_URL" and "smtp" in w["message"]
                   for w in result["warnings"])

    def test_genuine_integration_reference_does_not_warn(self):
        compose = """\
services:
  app:
    image: x
    environment:
      MAIL_URL: "{{ smtp.host }}"
"""
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            result = run_preflight(compose)
        assert result["warnings"] == []

    def test_shape_error_surfaces_verbatim(self):
        compose = "services:\n  app:\n    image: x\n    privileged: true\n"
        with pytest.raises(PreflightError) as e:
            run_preflight(compose)
        row = e.value.errors[0]
        assert row["service"] == "app"
        assert row["key"] == "privileged"
        assert "not permitted" in row["message"]

    def test_invalid_yaml_is_a_named_error(self):
        with pytest.raises(PreflightError) as e:
            run_preflight("services: [unclosed")
        assert e.value.errors[0]["key"] == "compose_yaml"

    def test_alias_reference_refused_at_parse(self):
        """The preflight's own walks are alias-safe, but a GREEN
        preflight's bytes go on to /start/, whose render accounting
        walks in the parent process — refusing the reference at load is
        what keeps green-preflight bytes safe to relay (transport-PR
        adversarial review)."""
        bomb = ("services:\n  app:\n    image: nginx\n"
                "    environment:\n"
                "      a0: &a0 [x]\n"
                "      a1: &a1 [*a0, *a0]\n")
        with pytest.raises(PreflightError) as e:
            run_preflight(bomb)
        assert e.value.errors[0]["key"] == "compose_yaml"
        assert "alias" in e.value.errors[0]["message"]

    def test_unresolvable_image_is_an_error_naming_it(self):
        with patch.object(pf.compose_mod, "client") as client:
            client.images.pull.side_effect = RuntimeError("registry down")
            with pytest.raises(PreflightError) as e:
                run_preflight(_COMPOSE)
        row = e.value.errors[0]
        assert row["service"] == "app"
        assert row["key"] == "image"
        assert "ghcr.io/me/app:1.2.3" in row["message"]
        assert "digest" in row["message"]

    def test_bracket_form_config_reference_detected(self):
        compose = """\
services:
  app:
    image: x
    environment:
      DB_PASSWORD: "{{ config['DB_PASSWORD'] }}"
"""
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            result = run_preflight(compose)
        by_key = {c["key"]: c for c in result["config_candidates"]}
        assert by_key["DB_PASSWORD"]["referenced"] is True

    def test_alias_bomb_neutered_by_dedup(self):
        # codex review: alias doubling expanded exponentially during the
        # leaf walk (outside every render guard). The fix visits each
        # SHARED container once, so the exponential alias chain resolves
        # in linear visits — this must complete fast, not raise.
        #
        # Since the inline-transport PR, tenant composes with alias
        # REFERENCES are refused at parse (see
        # test_alias_reference_refused_at_parse) — this test feeds the
        # walker a DAG built with plain safe_load to keep proving the
        # dedup itself, as defense in depth for any future parse
        # boundary that lets aliases through.
        import time

        import yaml as _yaml

        compose = "services:\n  app:\n    image: x\n    labels:\n"
        prev_ref = None
        for i in range(20):  # 2^20 nodes if expanded naively
            if prev_ref is None:
                compose += f"      - &a{i} ['x']\n"
                prev_ref = f"*a{i}"
            else:
                compose += f"      - &a{i} [{prev_ref}, {prev_ref}]\n"
                prev_ref = f"*a{i}"
        # plain safe_load still constructs the shared-object DAG; the
        # walker under test is the preflight's own leaf iterator
        tree = _yaml.safe_load(compose)
        t0 = time.monotonic()
        leaves = list(pf._string_leaves(tree))  # completes
        assert time.monotonic() - t0 < 5.0
        # linear visits: 20 chain levels + the literal leaves, nothing
        # exponential
        assert len(leaves) < 50

    def test_visit_budget_catches_huge_structures(self):
        # A genuinely large NON-aliased structure (no dedup to save it)
        # must trip the visit budget with a named refusal.
        items = "\n".join(f"      - item{i}" for i in range(20050))
        compose = f"services:\n  app:\n    image: x\n    labels:\n{items}\n"
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            with pytest.raises(PreflightError) as e:
                run_preflight(compose)
        assert "too large" in e.value.errors[0]["message"]

    def test_imageless_service_refused(self):
        compose = "services:\n  app:\n    command: echo hi\n"
        with pytest.raises(PreflightError) as e:
            run_preflight(compose)
        assert e.value.errors[0]["key"] == "image"
        assert "cannot deploy" in e.value.errors[0]["message"]

    def test_config_ref_mid_expression_detected(self):
        compose = ("services:\n  app:\n    image: x\n"
                   "    environment:\n"
                   "      SECRET: \"{{ 'tok:' ~ config.SECRET }}\"\n")
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            result = run_preflight(compose)
        by_key = {c["key"]: c for c in result["config_candidates"]}
        assert by_key["SECRET"]["referenced"] is True

    def test_bracket_ref_no_spurious_quote_candidate(self):
        compose = ("services:\n  app:\n    image: x\n"
                   "    environment:\n"
                   '      DB_PASSWORD: "{{ config[\'DB_PASSWORD\'] }}"\n')
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            result = run_preflight(compose)
        keys = {c["key"] for c in result["config_candidates"]}
        assert "'" not in keys
        assert "DB_PASSWORD" in keys

    def test_reader_error_is_a_named_422_not_500(self):
        with pytest.raises(PreflightError) as e:
            run_preflight("services:\n  app:\n    image: \"x\x00y\"\n")
        assert e.value.errors[0]["key"] == "compose_yaml"

    def test_operator_grant_threaded_through(self):
        compose = ("services:\n  app:\n    image: x\n"
                   "    cap_add: [NET_ADMIN]\n")
        with patch.object(pf.compose_mod, "client") as client:
            client.images = _mock_images_ok()
            with pytest.raises(PreflightError):
                run_preflight(compose)  # strict refuses cap_add ungranted
            result = run_preflight(compose, extra_keys="cap_add")
        assert result["services"] == ["app"]


class TestPreflightRoute:
    @pytest.mark.asyncio
    async def test_route_200(self, client, monkeypatch):
        with patch.object(pf.compose_mod, "client") as client_docker:
            client_docker.images = _mock_images_ok()
            r = await client.post(
                "/api/controller/preflight/",
                json={"compose_yaml": _COMPOSE},
                headers={TOKEN_HEADER: "test-token"})
        assert r.status_code == 200
        body = r.json()
        assert body["services"] == ["app"]
        assert "image_digests" in body and "warnings" in body

    @pytest.mark.asyncio
    async def test_route_422_rows(self, client):
        r = await client.post(
            "/api/controller/preflight/",
            json={"compose_yaml": "services:\n  app:\n    image: x\n"
                                  "    devices: ['/dev/sda:/dev/sda']\n"},
            headers={TOKEN_HEADER: "test-token"})
        assert r.status_code == 422
        row = r.json()["errors"][0]
        assert row["key"] == "devices"

    @pytest.mark.asyncio
    async def test_route_requires_token(self, client):
        r = await client.post("/api/controller/preflight/",
                        json={"compose_yaml": "services: {}"})
        assert r.status_code in (401, 403)
