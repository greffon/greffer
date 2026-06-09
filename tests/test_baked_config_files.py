"""Tests for the baked-config-files feature: render-time templating of
`file`/`json` destinations and the `config` Jinja context.

Uses a REAL ``datauri.DataURI`` (not mocked) so the base64-bytes vs
percent-encoded-str decode paths are actually exercised — the legacy
``test_apply_configuration_file`` mocks ``DataURI.data = b'...'`` and would
miss the str-vs-bytes crash this feature hardens against.
"""
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from apps.utils.docker.compose import (
    ConfigRenderError,
    _compute_config_context,
    apply_configuration,
    build_render_context,
)


def _b64_datauri(text, mime="text/plain"):
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"data:{mime};base64,{payload}"


def _plain_datauri(text):
    # Percent-encoded (NON-base64) data-URI: DataURI(...).data returns str.
    return f"data:text/plain,{quote(text)}"


def _greffon_info(file_datauri, render=True, secret="s3kr3t"):
    """A greffon with one greffon-secret env config and one baked realm file
    that references both the instance URL and the minted secret."""
    return {
        "id": "inst-baked",
        "ports": [{"url": "https://app.example.com", "port_host": 9000}],
        "configurations": [
            {
                "value": {"value": secret},
                "destinations": [
                    {"type": "env", "container": "backend", "key": "OIDC_RP_CLIENT_SECRET"}
                ],
            },
            {
                "value": {"file": file_datauri},
                "destinations": [
                    {
                        "type": "file",
                        "name": "realm.json",
                        "volume": "kc_import",
                        **({"x-greffon-render": True} if render else {}),
                    }
                ],
            },
        ],
        "volumes": {"kc_import": {"files": []}},
    }


_REALM_TEMPLATE = (
    '{"url": "{{ instance_url }}", '
    '"secret": "{{ config.OIDC_RP_CLIENT_SECRET }}"}'
)


class ConfigContextTests(unittest.TestCase):
    def test_env_values_exposed_under_config_namespace(self):
        info = _greffon_info(_b64_datauri(_REALM_TEMPLATE))
        build_render_context(info)
        self.assertEqual(info["config"]["OIDC_RP_CLIENT_SECRET"], "s3kr3t")

    def test_non_dict_value_does_not_raise(self):
        # A malformed payload must not 500 a deploy that works today.
        info = {
            "id": "x",
            "ports": [],
            "configurations": [
                {"value": "not-a-dict", "destinations": [{"type": "env", "key": "K"}]},
                {"value": None, "destinations": [{"type": "env", "key": "K2"}]},
                {"value": {"value": "ok"}, "destinations": [{"type": "env", "key": "K3"}]},
            ],
        }
        _compute_config_context(info)
        self.assertEqual(info["config"], {"K3": "ok"})

    def test_config_is_idempotent_setdefault(self):
        info = {"id": "x", "config": {"PRESET": "1"}, "configurations": []}
        _compute_config_context(info)
        self.assertEqual(info["config"], {"PRESET": "1"})


class RenderFileTests(unittest.TestCase):
    def _run(self, info, compose=None):
        if compose is None:
            # The fixture's env destination targets the `backend` service.
            compose = {"services": {"backend": {"environment": {}}}}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GREFFON_PATH": tmp}):
                with patch("apps.utils.docker.compose.remove_compose_file"):
                    build_render_context(info)
                    apply_configuration(info, compose)
                    path = os.path.join(tmp, info["id"], "realm.json")
                    with open(path, "rb") as f:
                        return f.read()

    def test_render_substitutes_url_and_secret(self):
        info = _greffon_info(_b64_datauri(_REALM_TEMPLATE))
        out = self._run(info).decode("utf-8")
        self.assertEqual(
            json.loads(out),
            {"url": "https://app.example.com", "secret": "s3kr3t"},
        )
        self.assertNotIn("{{", out)

    def test_rendered_secret_matches_container_env(self):
        # One source of truth: the realm file secret == the backend env secret.
        info = _greffon_info(_b64_datauri(_REALM_TEMPLATE))
        compose = {"services": {"backend": {"environment": {}}}}
        out = self._run(info, compose).decode("utf-8")
        rendered_secret = json.loads(out)["secret"]
        env_secret = compose["services"]["backend"]["environment"]["OIDC_RP_CLIENT_SECRET"]
        self.assertEqual(rendered_secret, env_secret, "s3kr3t")

    def test_unflagged_file_written_verbatim_base64(self):
        # Non-rendered file keeps literal Jinja markers, byte-identical.
        info = _greffon_info(_b64_datauri(_REALM_TEMPLATE), render=False)
        out = self._run(info)
        self.assertEqual(out, _REALM_TEMPLATE.encode("utf-8"))
        self.assertIn(b"{{", out)

    def test_unflagged_file_non_base64_datauri_str(self):
        # Percent-encoded data-URI → DataURI.data is str; must not crash on
        # the binary write path (the legacy code assumed bytes).
        info = _greffon_info(_plain_datauri("hello {{ x }}"), render=False)
        out = self._run(info)
        self.assertEqual(out, b"hello {{ x }}")

    def test_render_non_base64_datauri_str(self):
        info = _greffon_info(_plain_datauri(_REALM_TEMPLATE), render=True)
        out = self._run(info).decode("utf-8")
        self.assertEqual(json.loads(out)["secret"], "s3kr3t")

    def test_missing_variable_raises_config_render_error(self):
        bad = '{"secret": "{{ config.OIDC_RP_CLIENT_SECRET_TYPO }}"}'
        info = _greffon_info(_b64_datauri(bad), render=True)
        with self.assertRaises(ConfigRenderError):
            self._run(info)


class RenderJsonTests(unittest.TestCase):
    def test_json_render_when_flagged(self):
        info = {
            "id": "inst-json",
            "ports": [{"url": "https://app.example.com"}],
            "configurations": [
                {
                    "value": {"public_url": "{{ instance_url }}"},
                    "destinations": [
                        {
                            "type": "json",
                            "name": "settings.json",
                            "volume": "v",
                            "x-greffon-render": True,
                        }
                    ],
                }
            ],
            "volumes": {"v": {"files": []}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GREFFON_PATH": tmp}):
                build_render_context(info)
                apply_configuration(info, {})
                with open(os.path.join(tmp, "inst-json", "settings.json")) as f:
                    data = json.load(f)
        self.assertEqual(data["public_url"], "https://app.example.com")


if __name__ == "__main__":
    unittest.main()
