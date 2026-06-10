# Tests for Feature #4 of the integrations epic — greffer side.
# Covers:
#   - Pydantic GreffonStartRequest accepts/defaults the new
#     `integrations` field (and the wire-compat story: missing field
#     and unknown extras are tolerated).
#   - _is_integration_set classifier (the unset/empty/set boundary).
#   - _compute_integrations_context lifts each known type into the
#     Jinja-context shape.
#   - _delete_unset_integration_env_keys strips catalog-declared SMTP
#     env keys when the user didn't pick an SMTP integration, in both
#     mapping- and list-form `environment:` blocks.
#   - End-to-end Jinja render: with the integration set, `{{ smtp.host }}`
#     resolves to the dict's host; with it unset, the env key is gone.

from unittest import TestCase

import yaml
from jinja2 import Template
from pydantic import ValidationError

from app.models.controller import GreffonStartRequest
from apps.utils.docker.compose import (
    KNOWN_INTEGRATION_TYPES,
    _compute_integrations_context,
    _delete_unset_integration_env_keys,
    _is_integration_set,
    create_compose,
)
from apps.utils.greffon.repository import create_greffon_info


_VALID_CERT = {'certificate': 'CERT-PEM', 'private_key': 'KEY-PEM'}


def _smtp_destinations():
    return [
        {'type': 'smtp', 'key': 'SMTP_HOST_ADDR', 'container': 'plausible'},
        {'type': 'smtp', 'key': 'SMTP_HOST_PORT', 'container': 'plausible'},
        {'type': 'smtp', 'key': 'SMTP_USER_NAME', 'container': 'plausible'},
        {'type': 'smtp', 'key': 'SMTP_USER_PWD', 'container': 'plausible'},
        {'type': 'smtp', 'key': 'SMTP_HOST_SSL_ENABLED', 'container': 'plausible'},
        {'type': 'smtp', 'key': 'MAILER_EMAIL', 'container': 'plausible'},
    ]


def _greffon_info_with_smtp_destinations():
    return {
        'id': 'inst-1',
        'configurations': [
            {'value': {}, 'destinations': _smtp_destinations()},
        ],
        'integrations': {},
    }


class GreffonStartRequestIntegrationsFieldTests(TestCase):
    """Wire-compat: the new `integrations` field is optional, defaults
    to {}, and unknown top-level fields are silently ignored."""

    def _base(self, **overrides):
        base = {
            'id': 'inst-1',
            'repository_url': 'https://example.com/compose.yml',
            'cert': _VALID_CERT,
            'configurations': [],
            'ports': {},
        }
        base.update(overrides)
        return base

    def test_defaults_to_empty_dict_when_omitted(self):
        # Old manager → new greffer: payload omits the field. Must not
        # 422 and must default to {} so the render path treats it as
        # "no integrations selected".
        req = GreffonStartRequest(**self._base())
        self.assertEqual(req.integrations, {})

    def test_accepts_smtp_block(self):
        smtp = {'host': 'smtp.example.com', 'port': 587}
        req = GreffonStartRequest(**self._base(integrations={'smtp': smtp}))
        self.assertEqual(req.integrations, {'smtp': smtp})

    def test_extra_top_level_field_ignored(self):
        # Future manager → current greffer: unknown top-level keys
        # don't 422. `model_config = {'extra': 'ignore'}` enforces this.
        req = GreffonStartRequest(**self._base(future_field={'whatever': 1}))
        self.assertFalse(hasattr(req, 'future_field'))

    def test_rejects_non_dict_integrations(self):
        with self.assertRaises(ValidationError):
            GreffonStartRequest(**self._base(integrations='nope'))

    def test_l4_endpoint_fields_default_none(self):
        # Old (pre-Gap-2) manager → new greffer: the tunnel L4 hand-off
        # fields are omitted and default to None, so the controller falls
        # back to the empty-in-tunnel behavior.
        req = GreffonStartRequest(**self._base())
        self.assertIsNone(req.instance_l4_host)
        self.assertIsNone(req.instance_l4_port)
        self.assertIsNone(req.instance_l4_proto)

    def test_accepts_l4_endpoint_fields(self):
        req = GreffonStartRequest(**self._base(
            instance_l4_host='tunnel.greffon.io',
            instance_l4_port=20007,
            instance_l4_proto='udp',
        ))
        self.assertEqual(req.instance_l4_host, 'tunnel.greffon.io')
        self.assertEqual(req.instance_l4_port, 20007)
        self.assertEqual(req.instance_l4_proto, 'udp')


class IsIntegrationSetTests(TestCase):
    """The unset/empty/set boundary that the render pipeline relies on
    to decide whether to expose the integration to Jinja and skip the
    delete-on-unset env-key strip."""

    def test_non_empty_dict_is_set(self):
        self.assertTrue(_is_integration_set({'host': 'x'}))

    def test_empty_dict_is_unset(self):
        # Empty config means user didn't fill anything — treat as
        # "didn't pick this integration" so we don't render half-
        # configured env vars that fail at first send.
        self.assertFalse(_is_integration_set({}))

    def test_none_is_unset(self):
        self.assertFalse(_is_integration_set(None))

    def test_non_dict_is_unset(self):
        # Defensive — pydantic should already reject this on the wire,
        # but in-memory callers can pass anything.
        self.assertFalse(_is_integration_set('host=smtp.x'))
        self.assertFalse(_is_integration_set(['host', 'smtp.x']))


class ComputeIntegrationsContextTests(TestCase):
    """Lifts integration types from `greffon_info['integrations']` into
    top-level Jinja-visible keys (one per known type)."""

    def test_set_smtp_lifted_to_top_level(self):
        info = {
            'id': 'inst-1',
            'integrations': {'smtp': {'host': 'smtp.x', 'port': 587}},
        }
        out = _compute_integrations_context(info)
        self.assertEqual(out['smtp'], {'host': 'smtp.x', 'port': 587})

    def test_unset_smtp_becomes_empty_dict(self):
        # Empty dict (not None) so Jinja's getitem returns Undefined
        # which prints to '' rather than blowing up on attribute access.
        info = {'id': 'inst-1', 'integrations': {}}
        out = _compute_integrations_context(info)
        self.assertEqual(out['smtp'], {})

    def test_missing_integrations_key_handled(self):
        info = {'id': 'inst-1'}
        out = _compute_integrations_context(info)
        for t in KNOWN_INTEGRATION_TYPES:
            self.assertEqual(out[t], {})

    def test_does_not_clobber_pre_existing_top_level_key(self):
        # setdefault semantics: a caller (or a future feature) that
        # already populated `greffon_info['smtp']` for some other reason
        # should win.
        info = {
            'id': 'inst-1',
            'integrations': {'smtp': {'host': 'wire'}},
            'smtp': {'host': 'preset'},
        }
        out = _compute_integrations_context(info)
        self.assertEqual(out['smtp'], {'host': 'preset'})


class DeleteUnsetIntegrationEnvKeysTests(TestCase):
    """The post-render-time guarantee: declared SMTP env keys disappear
    from the compose dict entirely when the user didn't pick SMTP."""

    def _compose_with_smtp_env(self, env_form='dict'):
        smtp_keys = [d['key'] for d in _smtp_destinations()]
        if env_form == 'dict':
            env = {k: '{{ smtp.host }}' for k in smtp_keys}
        else:
            env = [f'{k}={{{{ smtp.host }}}}' for k in smtp_keys]
        return {
            'services': {
                'plausible': {'environment': env, 'image': 'plausible/analytics:latest'},
                'unrelated': {'environment': {'KEEP': 'me'}},
            },
        }

    def test_unset_smtp_strips_keys_mapping_form(self):
        compose = self._compose_with_smtp_env('dict')
        info = _compute_integrations_context(_greffon_info_with_smtp_destinations())
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['plausible']['environment'], {})
        # Unrelated env on a sibling service must not be touched.
        self.assertEqual(compose['services']['unrelated']['environment'], {'KEEP': 'me'})

    def test_unset_smtp_strips_keys_list_form(self):
        compose = self._compose_with_smtp_env('list')
        info = _compute_integrations_context(_greffon_info_with_smtp_destinations())
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['plausible']['environment'], [])

    def test_set_smtp_leaves_keys_in_place(self):
        compose = self._compose_with_smtp_env('dict')
        info = _greffon_info_with_smtp_destinations()
        info['integrations'] = {'smtp': {'host': 'smtp.x', 'port': 587}}
        info = _compute_integrations_context(info)
        _delete_unset_integration_env_keys(compose, info)
        # Every declared key still present; values stay templated for
        # Jinja to substitute downstream.
        for k in (d['key'] for d in _smtp_destinations()):
            self.assertIn(k, compose['services']['plausible']['environment'])

    def test_no_destinations_for_smtp_is_a_noop(self):
        # Greffon doesn't reference SMTP at all (e.g. freqtrade) —
        # nothing to strip even though smtp is unset.
        compose = {'services': {'app': {'environment': {'KEEP': 'me'}}}}
        info = {
            'id': 'inst-1',
            'configurations': [
                {'destinations': [{'type': 'env', 'key': 'KEEP', 'container': 'app'}]},
            ],
            'integrations': {},
        }
        info = _compute_integrations_context(info)
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['app']['environment'], {'KEEP': 'me'})

    def test_destination_for_missing_service_is_skipped(self):
        # Defensive: if metadata declares a destination for a container
        # name not present in the compose, we shouldn't KeyError.
        compose = {'services': {'plausible': {'environment': {}}}}
        info = {
            'id': 'inst-1',
            'configurations': [
                {'destinations': [{'type': 'smtp', 'key': 'X', 'container': 'ghost'}]},
            ],
            'integrations': {},
        }
        info = _compute_integrations_context(info)
        # Should not raise.
        _delete_unset_integration_env_keys(compose, info)


class GreffonInfoIntegrationsThreadingTests(TestCase):
    """`create_greffon_info` must surface the start-request's
    `integrations` field into greffon_info so the render pipeline can
    see it."""

    def test_integrations_threaded_through(self):
        compose = {'services': {'plausible': {'image': 'x'}}}
        greffon = {
            'id': 'inst-1',
            'configurations': [],
            'cert': _VALID_CERT,
            'ports': [],
            'integrations': {'smtp': {'host': 'smtp.x'}},
        }
        info = create_greffon_info(compose, greffon)
        self.assertEqual(info['integrations'], {'smtp': {'host': 'smtp.x'}})

    def test_missing_integrations_defaults_to_empty_dict(self):
        # Wire-compat: an old manager's payload won't have the key.
        compose = {'services': {'plausible': {'image': 'x'}}}
        greffon = {
            'id': 'inst-1', 'configurations': [], 'cert': _VALID_CERT, 'ports': [],
        }
        info = create_greffon_info(compose, greffon)
        self.assertEqual(info['integrations'], {})


class CreateComposeRenderEndToEndTests(TestCase):
    """Full Jinja-render path with mocked filesystem — confirms the
    catalog story works end-to-end on greffer side."""

    def _render(self, integrations):
        # Build a minimal compose with an SMTP env var templated on smtp.host.
        compose = {
            'services': {
                'plausible': {
                    'environment': {'SMTP_HOST_ADDR': '{{ smtp.host }}'},
                },
            },
        }
        info = {
            'id': 'inst-1',
            'configurations': [
                {
                    'destinations': [
                        {'type': 'smtp', 'key': 'SMTP_HOST_ADDR', 'container': 'plausible'},
                    ],
                },
            ],
            'integrations': integrations,
            'ports': [{'port_host': 4242}],
        }
        # Don't actually touch the filesystem — patch out makedirs/open.
        from unittest.mock import patch, mock_open
        with patch('apps.utils.docker.compose.os.makedirs'), \
             patch('apps.utils.docker.compose.os.path.exists', return_value=True), \
             patch('builtins.open', mock_open()) as m:
            create_compose(compose, info)
        # The rendered YAML string is the only argument to the open
        # context manager's write call.
        rendered = m().write.call_args[0][0]
        return rendered

    def test_set_smtp_substitutes_host(self):
        # yaml.dump quotes string values that look domain-y; we accept
        # either quoting style — what matters is the substituted value.
        rendered = self._render({'smtp': {'host': 'smtp.mailgun.org', 'port': 587}})
        self.assertIn('smtp.mailgun.org', rendered)
        self.assertIn('SMTP_HOST_ADDR:', rendered)
        self.assertNotIn('{{ smtp.host }}', rendered)

    def test_unset_smtp_removes_env_key_entirely(self):
        rendered = self._render({})
        self.assertNotIn('SMTP_HOST_ADDR', rendered)
