# Tests for the greffer half of platform-identity Feature #3 — making
# `oidc` a known integration type.
#
# The whole feature is one tuple entry, so these tests exist to pin the
# BEHAVIOUR that entry buys rather than the entry itself:
#   - `oidc` is lifted into the Jinja context, set or unset.
#   - A catalog entry referencing `{{ oidc.* }}` renders when the user
#     has no OIDC integration, instead of raising UndefinedError. That
#     is the regression the ordering trap in the epic describes, and
#     `test_unset_oidc_renders_instead_of_raising` is the one test that
#     fails if the tuple entry is removed.
#   - Adding the type does not disturb SMTP.

import itertools
import logging
import os
import pathlib
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import mock_open, patch

import yaml

from jinja2 import Environment, Template, UndefinedError, nodes
from jinja2.exceptions import TemplateAssertionError, TemplateSyntaxError

from apps.utils.docker import compose as compose_module
from apps.utils.docker.compose import (
    KNOWN_INTEGRATION_TYPES,
    ConfigRenderError,
    _UnsetIntegration,
    _compose_render_context,
    _UNDO_TIME_BUDGET,
    _UNDO_CHUNK,
    _GUARD_SLOTS,
    _document_renders,
    _items_removed,
    _render_baked_file,
    build_render_context,
    _compute_integrations_context,
    _delete_unset_integration_env_keys,
    create_compose,
)


_ISSUER = 'https://id.example.com/realms/main'


def _oidc_destinations():
    return [
        {'type': 'oidc', 'key': 'OIDC_ISSUER_URL', 'container': 'grafana'},
        {'type': 'oidc', 'key': 'OIDC_CLIENT_ID', 'container': 'grafana'},
    ]


def _greffon_info_with_oidc_destinations(integrations=None):
    return {
        'id': 'inst-1',
        'configurations': [{'destinations': _oidc_destinations()}],
        'integrations': integrations if integrations is not None else {},
    }


class OIDCIsAKnownTypeTests(TestCase):
    def test_oidc_is_registered(self):
        self.assertIn('oidc', KNOWN_INTEGRATION_TYPES)

    def test_smtp_is_still_registered(self):
        # Additive, not a replacement.
        self.assertIn('smtp', KNOWN_INTEGRATION_TYPES)


class ComputeOIDCContextTests(TestCase):
    def test_set_oidc_is_lifted_to_top_level(self):
        info = _greffon_info_with_oidc_destinations({'oidc': {'issuer': _ISSUER}})
        out = _compute_integrations_context(info)
        self.assertEqual(out['oidc'], {'issuer': _ISSUER})

    def test_unset_oidc_becomes_empty_dict(self):
        out = _compute_integrations_context(_greffon_info_with_oidc_destinations())
        self.assertEqual(out['oidc'], {})

    def test_empty_config_counts_as_unset(self):
        # `{}` means "user didn't pick it", same as absence — a
        # half-configured OIDC block must not reach the render.
        out = _compute_integrations_context(
            _greffon_info_with_oidc_destinations({'oidc': {}}),
        )
        self.assertEqual(out['oidc'], {})

    def test_oidc_and_smtp_are_independent(self):
        info = {
            'id': 'inst-1',
            'integrations': {'smtp': {'host': 'smtp.x'}},
        }
        out = _compute_integrations_context(info)
        self.assertEqual(out['smtp'], {'host': 'smtp.x'})
        self.assertEqual(out['oidc'], {})


class DeleteUnsetOIDCEnvKeysTests(TestCase):
    def _compose(self, env_form='dict'):
        keys = [d['key'] for d in _oidc_destinations()]
        if env_form == 'dict':
            env = {k: '{{ oidc.issuer }}' for k in keys}
        else:
            env = [f'{k}={{{{ oidc.issuer }}}}' for k in keys]
        return {
            'services': {
                'grafana': {'environment': env, 'image': 'grafana/grafana:latest'},
                'unrelated': {'environment': {'KEEP': 'me'}},
            },
        }

    def test_unset_oidc_strips_keys_mapping_form(self):
        compose = self._compose('dict')
        info = _compute_integrations_context(_greffon_info_with_oidc_destinations())
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['grafana']['environment'], {})
        self.assertEqual(compose['services']['unrelated']['environment'], {'KEEP': 'me'})

    def test_unset_oidc_strips_keys_list_form(self):
        compose = self._compose('list')
        info = _compute_integrations_context(_greffon_info_with_oidc_destinations())
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['grafana']['environment'], [])

    def test_set_oidc_leaves_keys_in_place(self):
        compose = self._compose('dict')
        info = _compute_integrations_context(
            _greffon_info_with_oidc_destinations({'oidc': {'issuer': _ISSUER}}),
        )
        _delete_unset_integration_env_keys(compose, info)
        for k in (d['key'] for d in _oidc_destinations()):
            self.assertIn(k, compose['services']['grafana']['environment'])
        # "Keys survive" alone is true whether or not `oidc` is a known
        # type -- an unknown type is never stripped either -- so it does
        # not distinguish this feature from its absence. The context
        # having been populated does.
        self.assertEqual(info['oidc'], {'issuer': _ISSUER})

    def test_bracket_form_reference_is_stripped(self):
        # `{{ oidc['issuer'] }}` is valid Jinja and semantically the same
        # as the attribute form, so pass 2's regex must catch it too.
        compose = {
            'services': {
                'grafana': {'environment': {'UNDECLARED': "{{ oidc['issuer'] }}"}},
            },
        }
        info = _compute_integrations_context({'id': 'inst-1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['grafana']['environment'], {})

    def test_unrelated_key_containing_oidc_substring_survives(self):
        # Three distinct guards, one case each, because they are NOT the
        # same rule:
        #   NOTE  -- the token is not followed by `.` or `[`.
        #   OTHER -- `\b` rejects `myoidc.issuer`.
        #   UPPER -- the pattern is case-SENSITIVE, which is what rejects
        #            `KEYCLOAK_OIDC.foo`; `\b` would not, since `_` is a
        #            word character and there is no boundary before it.
        compose = {
            'services': {
                'grafana': {
                    'environment': {
                        'NOTE': '{{ instance_id }} uses oidc somewhere',
                        'OTHER': '{{ myoidc.issuer }}',
                        'UPPER': '{{ KEYCLOAK_OIDC.foo }}',
                    },
                },
            },
        }
        info = _compute_integrations_context({'id': 'inst-1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(
            set(compose['services']['grafana']['environment']),
            {'NOTE', 'OTHER', 'UPPER'},
        )

    def test_literal_provider_host_beside_an_unrelated_interpolation_survives(self):
        # The regression that adding `oidc` to the tuple would otherwise
        # introduce. `oidc.acme.com` here is a LITERAL hostname, and the
        # `{{ ... }}` in the same value interpolates something else
        # entirely. Popping these leaves the greffon booting without its
        # issuer and failing at runtime with nothing to trace it to.
        #
        # This is the normal shape for an OIDC entry -- a provider at
        # `oidc.<domain>` plus a per-instance redirect URI -- which is
        # why the pre-existing whole-string match had to be scoped to the
        # expression body before this type could join the tuple.
        compose = {
            'services': {
                'grafana': {
                    'environment': {
                        'ISSUER': 'https://oidc.acme.com/realms/{{ instance_id }}',
                        'ALLOWED': '["{{ instance_url }}", "https://oidc.acme.com"]',
                        'SMTP_NOTE': 'relay smtp.acme.com for {{ instance_id }}',
                    },
                },
            },
        }
        info = _compute_integrations_context({'id': 'inst-1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(
            set(compose['services']['grafana']['environment']),
            {'ISSUER', 'ALLOWED', 'SMTP_NOTE'},
        )

    def test_real_reference_beside_an_unrelated_interpolation_is_still_stripped(self):
        # The other side of the same boundary: scoping to the expression
        # body must not stop a genuine reference being found when it
        # shares the value with another interpolation.
        compose = {
            'services': {
                'grafana': {
                    'environment': {
                        'REDIRECT': '{{ oidc.issuer }}/callback?i={{ instance_id }}',
                    },
                },
            },
        }
        info = _compute_integrations_context({'id': 'inst-1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['grafana']['environment'], {})


class PartiallyConfiguredIntegrationTests(TestCase):
    """A blob with some fields but not others.

    `smtp` is resolved by the manager from one fixed field set, so this
    barely happens there. The per-instance OIDC client registration this
    type exists for sends a blob whose key set varies by provider, so it
    is the NORMAL state for `oidc`.

    A partial blob is CONFIGURED, so the binding deliberately does not
    apply to it and every case below matches `main` exactly. A catalog
    entry that reads a field its provider may not send must say so with
    `|default`; the platform does not guess. See the docstring on
    `_compose_render_context` for why quiet truncation was rejected.
    """

    PARTIAL = {'oidc': {'client_id': 'x'}}

    def _render(self, template, integrations):
        info = _compute_integrations_context({'id': 'i1', 'integrations': integrations})
        return Template(template).render(**_compose_render_context(info))

    def _main(self, template, integrations):
        """What `main` renders: the raw blob, no binding at all."""
        return Template(template).render(**integrations)

    def _assert_matches_main(self, template, integrations):
        try:
            expected, raised = self._main(template, integrations), None
        except UndefinedError as exc:
            expected, raised = None, str(exc)
        if raised is None:
            self.assertEqual(self._render(template, integrations), expected)
        else:
            with self.assertRaises(UndefinedError) as caught:
                self._render(template, integrations)
            self.assertEqual(str(caught.exception), raised)

    def test_a_present_field_is_untouched(self):
        self.assertEqual(self._render('{{ oidc.client_id }}', self.PARTIAL), 'x')
        self._assert_matches_main('{{ oidc.client_id }}', self.PARTIAL)

    def test_an_absent_field_renders_empty(self):
        # Jinja's own default for a one-level miss, on `main` too.
        self.assertEqual(self._render('{{ oidc.issuer }}', self.PARTIAL), '')
        self._assert_matches_main('{{ oidc.issuer }}', self.PARTIAL)

    def test_chaining_off_an_absent_field_raises_like_main(self):
        # The chosen policy: a configured integration keeps main's LOUD
        # failure. Wrapping this too would render '' for a mistyped field
        # name on a working integration -- `nextcloud` ships
        # `{{ smtp.from_address.split("@")[1] }}`, and a rename there
        # would have shipped MAIL_DOMAIN="" to every instance instead of
        # refusing to start.
        with self.assertRaises(UndefinedError) as caught:
            self._render('{{ oidc.issuer.split("/")[0] }}', self.PARTIAL)
        self.assertIn('issuer', str(caught.exception))
        self._assert_matches_main('{{ oidc.issuer.split("/")[0] }}', self.PARTIAL)

    def test_default_is_the_supported_way_to_read_an_optional_field(self):
        # What a catalog entry for a varying-shape provider blob must do.
        self.assertEqual(
            self._render('{{ oidc.issuer|default("https://d") }}', self.PARTIAL),
            'https://d',
        )

    def test_a_fully_configured_blob_is_unchanged(self):
        full = {'oidc': {'issuer': 'https://id.example/x', 'client_id': 'x'}}
        self.assertEqual(self._render('{{ oidc.issuer }}', full), 'https://id.example/x')
        self.assertEqual(self._render('{{ oidc.issuer.split("/")[0] }}', full), 'https:')

    def test_it_is_still_truthy_when_configured_and_falsy_when_not(self):
        # Wrapping must not change what a `{% if oidc %}` guard decides.
        guard = '{% if oidc %}on{% else %}off{% endif %}'
        self.assertEqual(self._render(guard, self.PARTIAL), 'on')
        self.assertEqual(self._render(guard, {}), 'off')

    def test_a_configured_blob_still_serialises_as_itself(self):
        self.assertEqual(
            self._render('{{ oidc|tojson }}', self.PARTIAL), '{"client_id": "x"}',
        )


class ConfiguredIntegrationsAreNotBoundTests(TestCase):
    """The binding is unset-only, asserted at the context level.

    `PartiallyConfiguredIntegrationTests` pins the rendered consequence;
    this pins the mechanism, so a future change that starts wrapping
    configured types again fails here first and by name.
    """

    def _ctx(self, integrations):
        return _compose_render_context(
            _compute_integrations_context({'id': 'i1', 'integrations': integrations}),
        )

    def test_a_configured_type_stays_a_plain_dict(self):
        ctx = self._ctx({'smtp': {'host': 'h'}})
        self.assertNotIsInstance(ctx['smtp'], _UnsetIntegration)
        self.assertEqual(ctx['smtp'], {'host': 'h'})

    def test_a_partial_type_stays_a_plain_dict(self):
        ctx = self._ctx({'oidc': {'client_id': 'x'}})
        self.assertNotIsInstance(ctx['oidc'], _UnsetIntegration)

    def test_an_unset_type_is_bound(self):
        self.assertIsInstance(self._ctx({})['oidc'], _UnsetIntegration)

    def test_an_empty_blob_counts_as_unset_and_is_bound(self):
        self.assertIsInstance(self._ctx({'oidc': {}})['oidc'], _UnsetIntegration)

    def test_one_type_configured_does_not_unbind_the_other(self):
        ctx = self._ctx({'smtp': {'host': 'h'}})
        self.assertNotIsInstance(ctx['smtp'], _UnsetIntegration)
        self.assertIsInstance(ctx['oidc'], _UnsetIntegration)


class TheBindingDoesNotEscapeIntoGreffonInfoTests(TestCase):
    """`_compose_render_context` copies; it must not mutate its argument.

    This is the invariant the module argues hardest for and the one a
    single character (`dict(greffon_info)` -> `greffon_info`) silently
    voids. `_render_baked_file` renders with StrictUndefined so that a
    baked config file referencing an unset integration REFUSES (422)
    instead of being written out with the value blanked. It reads the
    same `greffon_info`. If the forgiving binding leaked back into it,
    that refusal would quietly become an empty secret in a config file
    on disk, which is the exact failure mode the strictness exists to
    prevent -- and no other test in this file would notice.
    """

    def _info(self):
        return _compute_integrations_context({'id': 'i1', 'integrations': {}})

    def test_the_argument_is_not_mutated(self):
        info = self._info()
        _compose_render_context(info)
        self.assertNotIsInstance(info['oidc'], _UnsetIntegration)
        self.assertEqual(info['oidc'], {})

    def test_the_returned_context_is_a_different_object(self):
        info = self._info()
        self.assertIsNot(_compose_render_context(info), info)

    def test_the_baked_file_path_still_refuses_an_unset_reference(self):
        # The consequence, end to end: rendering a baked file after
        # building a render context must still raise, not blank the value.
        info = self._info()
        _compose_render_context(info)
        with self.assertRaises(ConfigRenderError):
            _render_baked_file('{{ oidc.client_secret }}', info, 'realm.json')


class PassOneStillPopsFromMetadataTests(TestCase):
    """Pass 1 (manager-sent destinations) needs a NON-templated value.

    Every other test in this file uses a `{{ ... }}` value, which pass 2
    pops on its own -- so pass 1 could be deleted outright and they would
    all still pass. These use a literal value that only pass 1 can see.
    """

    def _run(self, env, destinations, container='app'):
        compose = {'services': {container: {'environment': env}}}
        _delete_unset_integration_env_keys(compose, {
            'id': 'i1', 'integrations': {},
            'configurations': [{'destinations': destinations}],
        })
        return compose['services'][container]['environment']

    def _dest(self, **kw):
        d = {'type': 'smtp', 'container': 'app', 'key': 'SMTP_HOST'}
        d.update(kw)
        return d

    def test_a_literal_value_is_popped_by_its_destination(self):
        self.assertEqual(self._run({'SMTP_HOST': 'mail.example'}, [self._dest()]), {})

    def test_a_literal_value_is_popped_in_list_form_too(self):
        self.assertEqual(self._run(['SMTP_HOST=mail.example'], [self._dest()]), [])

    def test_only_the_exact_key_is_popped(self):
        # `startswith` on the bare key would also eat SMTP_HOSTNAME.
        env = {'SMTP_HOST': 'a', 'SMTP_HOSTNAME': 'b'}
        self.assertEqual(self._run(env, [self._dest()]), {'SMTP_HOSTNAME': 'b'})

    def test_only_the_exact_key_is_popped_in_list_form(self):
        env = ['SMTP_HOST=a', 'SMTP_HOSTNAME=b']
        self.assertEqual(self._run(env, [self._dest()]), ['SMTP_HOSTNAME=b'])

    def test_a_destination_for_a_configured_type_is_left_alone(self):
        compose = {'services': {'app': {'environment': {'SMTP_HOST': 'mail'}}}}
        _delete_unset_integration_env_keys(compose, {
            'id': 'i1', 'integrations': {'smtp': {'host': 'mail'}},
            'configurations': [{'destinations': [self._dest()]}],
        })
        self.assertEqual(compose['services']['app']['environment'], {'SMTP_HOST': 'mail'})

    def test_a_destination_naming_another_container_is_ignored(self):
        env = {'SMTP_HOST': 'a'}
        self.assertEqual(
            self._run(env, [self._dest(container='other')]), {'SMTP_HOST': 'a'},
        )

    def test_a_destination_missing_its_key_or_container_is_skipped(self):
        for bad in ({'type': 'smtp', 'container': 'app'},
                    {'type': 'smtp', 'key': 'SMTP_HOST'},
                    {'type': 'smtp', 'container': '', 'key': 'SMTP_HOST'}):
            with self.subTest(bad=bad):
                self.assertEqual(self._run({'SMTP_HOST': 'a'}, [bad]), {'SMTP_HOST': 'a'})

    def test_a_non_dict_destination_does_not_crash(self):
        self.assertEqual(self._run({'SMTP_HOST': 'a'}, ['nonsense', None]), {'SMTP_HOST': 'a'})


class MalformedComposeShapesDoNotCrashTests(TestCase):
    """`/start/` must not 500 on a compose shape the catalog can express.

    Each of these raised an uncaught AttributeError/IndexError out of
    `create_compose` when its guard was removed.
    """

    def _run(self, compose):
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {}},
        )
        return compose

    def test_a_non_dict_service_is_skipped(self):
        self.assertEqual(
            self._run({'services': {'app': 'notadict'}}),
            {'services': {'app': 'notadict'}},
        )

    def test_a_bare_passthrough_env_entry_is_kept(self):
        # `environment: [SMTP_HOST]` is legal compose -- it passes the
        # host's variable through, and has no `=`.
        compose = self._run({'services': {'app': {'environment': ['SMTP_HOST']}}})
        self.assertEqual(compose['services']['app']['environment'], ['SMTP_HOST'])

    def test_a_scalar_that_is_not_a_string_is_kept(self):
        # YAML gives `K: 1` an int and `K: ~` None. Neither can carry a
        # reference, and stringifying instead of skipping would scan
        # their repr for Jinja.
        compose = self._run({'services': {'app': {'environment': {
            'N': 1, 'B': True, 'NONE': None,
        }}}})
        self.assertEqual(
            compose['services']['app']['environment'],
            {'N': 1, 'B': True, 'NONE': None},
        )

    def test_a_container_valued_env_var_IS_scanned(self):
        # `yaml.dump` turns a list or mapping value into template text
        # just like a scalar, so skipping non-strings left
        # `L: ['{{ smtp.host }}']` to render `['']` -- present but
        # empty, the failure this pass exists to stop.
        compose = self._run({'services': {'app': {'environment': {
            'L': ['{{ smtp.host }}'],
            'M': {'k': '{{ smtp.host }}'},
            'DEEP': [{'k': ['{{ oidc.issuer }}']}],
            'PLAIN': ['nothing templated'],
        }}}})
        self.assertEqual(
            compose['services']['app']['environment'],
            {'PLAIN': ['nothing templated']},
        )

    def test_a_service_without_an_environment_block_is_skipped(self):
        self.assertEqual(
            self._run({'services': {'app': {'image': 'nginx'}}}),
            {'services': {'app': {'image': 'nginx'}}},
        )

    def test_a_compose_without_services_is_skipped(self):
        self.assertEqual(self._run({}), {})

    def test_a_garbage_integration_blob_counts_as_unset(self):
        # `integrations={'smtp': 'garbage'}` is not a usable mapping, so
        # it must be treated as unset (popped + bound), not passed to
        # Jinja where `{{ smtp.host }}` would raise on a str.
        compose = {'services': {'app': {'environment': {'H': '{{ smtp.host }}'}}}}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {'smtp': 'garbage'}},
        )
        self.assertEqual(compose['services']['app']['environment'], {})

    def test_a_garbage_blob_is_normalised_out_of_the_context(self):
        # Not just popped -- the context must not carry the garbage
        # through to Jinja, where `{{ smtp.host }}` on a str raises.
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': {'smtp': 'garbage'}},
        )
        self.assertEqual(info['smtp'], {})
        self.assertEqual(
            Template('{{ smtp.host }}').render(**_compose_render_context(info)), '',
        )


class ListFormPassTwoTests(TestCase):
    """Pass 2 over list-form `environment:`, which nothing asserted."""

    def _run(self, env):
        compose = {'services': {'app': {'environment': env}}}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {'smtp': {'host': 'h'}}},
        )
        return compose['services']['app']['environment']

    def test_a_referencing_entry_is_dropped_and_the_rest_kept(self):
        self.assertEqual(
            self._run(['ISS={{ oidc.issuer }}', 'X=1', 'Y={{ instance_url }}']),
            ['X=1', 'Y={{ instance_url }}'],
        )

    def test_a_configured_type_is_not_dropped_from_list_form(self):
        self.assertEqual(self._run(['H={{ smtp.host }}']), ['H={{ smtp.host }}'])

    def test_a_value_containing_an_equals_sign_survives_intact(self):
        self.assertEqual(self._run(['Q=a=b=c']), ['Q=a=b=c'])


class ParseFailureFallbackTests(TestCase):
    """What an unparseable value falls back to, and that nothing escapes.

    ONE rule: the value names the integration. A `{%`-carrying value
    used to pop as well, to stop a block being half-popped, but the
    document guard supersedes that and the rule was removed because it
    also popped values referencing no integration at all (see
    `ASplitBlockIsLeftAloneTests`). So an unrelated unparseable value is
    not silently dropped from a deploy -- it fails the render loudly,
    exactly as it does on `main`.
    """

    def _kept(self, value):
        compose = {'services': {'app': {'environment': {'K': value}}}}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {'smtp': {'host': 'h'}}},
        )
        return 'K' in compose['services']['app']['environment']

    def test_an_unparseable_value_mentioning_the_name_is_popped(self):
        self.assertFalse(self._kept("{{ oidc.issuer'' }}"))

    def test_an_unparseable_value_not_mentioning_it_is_kept(self):
        self.assertTrue(self._kept("{{ 1 +* 2 }}"))

    def test_a_giant_integer_literal_does_not_escape_as_a_500(self):
        # `parse()` raises a bare ValueError (CPython's 4300-digit limit),
        # which is neither a TemplateError nor a RecursionError.
        self.assertTrue(self._kept('{{ 1' + '0' * 5000 + ' }}'))

    def test_the_giant_literal_really_does_raise_ValueError(self):
        # Pins the premise of the test above.
        from jinja2 import Environment
        with self.assertRaises(ValueError):
            Environment().parse('{{ 1' + '0' * 5000 + ' }}')

    def test_a_substring_of_the_name_does_not_count(self):
        self.assertTrue(self._kept("{{ notoidc.issuer'' }}"))
        self.assertTrue(self._kept("{{ oidcx.issuer'' }}"))

    def test_a_non_ascii_digit_does_not_escape_as_a_500(self):
        # Jinja's number lexer matches non-ASCII digits with a unicode
        # `\d`, then hands the literal to the CPython compiler, which
        # raises SyntaxError -- not a TemplateError, not a ValueError.
        for value in ('{{ 0.\uff11 }}', '{{ 0.\u0661 }}',
                      '{{ 1.0e\u0661 }}', '{{ 1.\u0665e\u0663 }}'):
            with self.subTest(value=value):
                self.assertTrue(self._kept(value))

    def test_the_non_ascii_digit_really_does_raise_SyntaxError(self):
        # Pins the premise: if Jinja stops accepting these, the test
        # above quietly stops covering anything.
        from jinja2 import Environment
        with self.assertRaises(SyntaxError):
            Environment().parse('{{ 0.\uff11 }}')

    def test_no_parser_exception_escapes_whatever_its_class(self):
        # The guard is deliberately bare, so pin the property rather than
        # a list of classes: a parse that blows up in ANY way must be
        # answered, not propagated.
        class Boom(Exception):
            pass

        with patch('apps.utils.docker.compose.Environment') as env_cls:
            env_cls.return_value.parse.side_effect = Boom('from the parser')
            try:
                kept = self._kept('{{ oidc.issuer }}')
            except Boom:
                self.fail('a parser exception escaped _dereferences')
        self.assertFalse(kept)

    def test_a_keyboard_interrupt_is_NOT_swallowed(self):
        # `Exception`, not `BaseException`: the broad catch is about the
        # parser failing, not about making the process unkillable.
        with patch('apps.utils.docker.compose.Environment') as env_cls:
            env_cls.return_value.parse.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                self._kept('{{ oidc.issuer }}')


class ASplitBlockIsLeftAloneTests(TestCase):
    """A `{% %}` block straddling two env values is not touched.

    An earlier attempt popped any unparseable value carrying a `{%` tag,
    so that a block could not be half-popped and left dangling. The
    document-level guard (see `TheStripNeverBreaksTheDocumentTests`)
    made that rule redundant and it was removed, because it also popped
    values that reference no integration at all -- `{% if instance_url %}`
    split across two values lost both keys on every instance with an
    unset integration, which today is every instance.

    So a half that names an integration is popped, the document is found
    unrenderable, and the pop is undone. The keys survive and render
    through the binding.
    """

    def _strip(self, env, integrations=None):
        compose = {'services': {'app': {'environment': dict(env)}}}
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': integrations or {}},
        )
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def _render(self, compose, info):
        return Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_a_split_block_keeps_both_halves_and_renders(self):
        env = {'A': '{% if oidc %}', 'B': 'x', 'C': '{% endif %}'}
        compose, info = self._strip(env)
        self.assertEqual(compose['services']['app']['environment'], env)
        self._render(compose, info)  # must not raise

    def test_a_block_naming_no_integration_is_not_touched(self):
        # The over-pop the removed rule caused: neither half references
        # an integration, so neither should be considered at all.
        env = {'A': '{% if instance_url %}', 'B': 'yes{% endif %}', 'KEEP': 'plain'}
        compose, info = self._strip(env, {'smtp': {'host': 'h'}})
        self.assertEqual(compose['services']['app']['environment'], env)

    def test_a_lone_end_tag_is_left_alone(self):
        env = {'C': '{% endif %}'}
        compose, _ = self._strip(env, {'smtp': {'host': 'h'}})
        self.assertEqual(compose['services']['app']['environment'], env)


class TheStripNeverBreaksTheDocumentTests(TestCase):
    """The document-level invariant, which no per-value rule can give.

    Both passes decide one env value at a time, but the whole compose is
    dumped into ONE Jinja template and rendered together. A construct
    split across two values therefore cannot be handled correctly value
    by value -- proven by these two shapes pulling in opposite
    directions:

      `{% raw %}` PARSES alone, so it is kept, while its `{% endraw %}`
      does not parse and is popped.

      `{# comment` holds no Jinja delimiter, so it is never examined,
      while the `{{ oidc.host }} #}` closing it is a real reference that
      MUST be popped.

    Either way the survivor is left dangling and the whole render
    raises, which is a 500 at `/start/` -- much worse than the env var
    the pop was worth. So the strip is checked as a whole and dropped if
    it broke the document.
    """

    def _run(self, env, integrations=None):
        compose = {'services': {'app': {'environment': dict(env)}}}
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': integrations or {}},
        )
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def _renders(self, compose, info):
        return Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_a_split_raw_block_is_left_alone_and_still_renders(self):
        env = {'A': '{% raw %}', 'B': '{% endraw %}'}
        compose, info = self._run(env)
        self.assertEqual(compose['services']['app']['environment'], env)
        self._renders(compose, info)  # must not raise

    def test_a_split_comment_is_left_alone_and_still_renders(self):
        env = {'A': '{# oidc note', 'B': '{{ oidc.host }} #}'}
        compose, info = self._run(env)
        self.assertEqual(compose['services']['app']['environment'], env)
        self._renders(compose, info)

    def test_an_ordinary_reference_is_still_popped(self):
        # The guard must not be so eager that it undoes the feature.
        compose, info = self._run({'H': '{{ oidc.issuer }}', 'X': '1'})
        self.assertEqual(compose['services']['app']['environment'], {'X': '1'})
        self._renders(compose, info)

    def test_the_glitchtip_shape_is_still_popped(self):
        # The case the strip exists for: present-but-empty renders
        # `smtp://:@:`, a malformed URL the app parses at boot.
        compose, info = self._run(
            {'EMAIL_URL': 'smtp://{{ smtp.username }}@{{ smtp.host }}'},
        )
        self.assertEqual(compose['services']['app']['environment'], {})

    def test_a_split_block_is_undone_but_a_clean_pop_is_kept(self):
        # The point of scoping the undo: `H` is a clean pop and survives,
        # while the straddling half that broke the document is restored.
        compose, info = self._run({
            'A': '{% if oidc %}', 'B': 'x', 'C': '{% endif %}',
            'H': '{{ oidc.issuer }}',
        })
        env = compose['services']['app']['environment']
        self.assertNotIn('H', env)
        self.assertEqual(sorted(env), ['A', 'B', 'C'])
        self._renders(compose, info)

    def test_an_already_broken_document_is_still_stripped(self):
        # If it did not parse BEFORE, the strip is not what broke it, and
        # refusing to strip would only lose the feature for no gain.
        compose, info = self._run({'H': '{{ oidc.issuer }}', 'BAD': '{% endfor %}'})
        self.assertNotIn('H', compose['services']['app']['environment'])

    def test_the_revert_is_logged(self):
        # A shape that actually triggers the undo: the closing half is a
        # real reference and IS popped, which orphans the opener.
        with self.assertLogs('apps.utils.docker.compose', level='WARNING') as caught:
            self._run({'A': '{# oidc note', 'B': '{{ oidc.host }} #}'})
        joined = ''.join(caught.output)
        self.assertIn('unrenderable', joined)
        self.assertIn('i1', joined)
        # The message must say what was kept and what was undone, or an
        # operator cannot tell a harmless undo from a lost env var.
        self.assertIn('kept', joined)
        self.assertIn('undid', joined)

    def test_a_compose_that_cannot_be_dumped_answers_no(self):
        # `_document_renders` must ANSWER for any input, never raise --
        # it runs on the `/start/` path. A generator makes `yaml.dump`
        # raise TypeError, which is not a Jinja error at all.
        self.assertFalse(
            _document_renders({'services': (i for i in [1])}, {}),
        )

    def test_an_uncopyable_compose_does_not_crash_the_strip(self):
        # Reachable: a module DUMPS (so the document parses and the
        # snapshot is attempted) but cannot be deepcopied. Without the
        # guard this is a TypeError straight out of `/start/`.
        import sys as _sys
        compose = {'services': {'app': {'environment': {'H': '{{ oidc.a }}'}}},
                   'x-mod': _sys}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {}},
        )
        self.assertEqual(compose['services']['app']['environment'], {})

    def test_an_undumpable_compose_does_not_crash_the_strip(self):
        compose = {'services': {'app': {'environment': {'H': '{{ oidc.a }}'}}},
                   'x-gen': (i for i in [1])}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {}},
        )
        # Nothing parsed before, so the strip proceeds as best effort.
        self.assertEqual(compose['services']['app']['environment'], {})


class ServicesThatIsNotAMappingTests(TestCase):
    """`services:` with an empty body parses to None, not {}."""

    def test_a_non_mapping_services_does_not_crash(self):
        for bad in (None, 'garbage', 7, [], ['app']):
            with self.subTest(bad=bad):
                compose = {'services': bad}
                _delete_unset_integration_env_keys(
                    compose, {'id': 'i1', 'integrations': {}},
                )
                self.assertEqual(compose, {'services': bad})

    def test_a_non_mapping_services_does_not_crash_in_pass_one(self):
        compose = {'services': None}
        _delete_unset_integration_env_keys(compose, {
            'id': 'i1', 'integrations': {},
            'configurations': [{'destinations': [
                {'type': 'oidc', 'container': 'app', 'key': 'K'},
            ]}],
        })
        self.assertEqual(compose, {'services': None})


class TheGuardAsksTheRenderQuestionTests(TestCase):
    """`_document_renders` must compile, not merely parse.

    The render is `Template(...)`, which parses AND compiles, and a
    family of `TemplateAssertionError`s comes only from the compile
    step. Popping a `{% raw %}`/`{% endraw %}` pair un-shields whatever
    sat between them, so the strip can ADD live constructs: a second
    `{% block b %}` becomes real, and the document fails to compile
    while parsing perfectly well. Asking the cheaper question let that
    through as a 500.
    """

    UNSHIELDED = {
        'A': '{% block b %}x{% endblock %}',
        'B': '{{ oidc.x }}{% raw %}',
        'C': '{% block b %}y{% endblock %}',
        'D': '{{ oidc.y }}{% endraw %}',
    }

    def test_a_compile_only_error_is_caught(self):
        compose = {'services': {'app': {'environment': dict(self.UNSHIELDED)}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_parsing_alone_would_not_have_caught_it(self):
        # Pins the premise: if these two ever agree, the test above
        # quietly stops covering anything.
        text = yaml.dump({'services': {'app': {'environment': {
            'A': '{% block b %}x{% endblock %}',
            'C': '{% block b %}y{% endblock %}',
        }}}})
        Environment().parse(text)  # parses happily
        with self.assertRaises(TemplateAssertionError):
            Environment().from_string(text)


class TheUndoIsScopedToWhatBrokeItTests(TestCase):
    """Undoing the WHOLE strip re-introduced the bug it exists to fix.

    The document is one template, but the damage is local. A malformed
    value in one service used to un-pop every correctly-popped key in
    every OTHER service, so glitchtip's `EMAIL_URL` came back and
    rendered `smtp://:@` -- the malformed URL its app parses at boot,
    and the whole reason the strip was written. "Weaker guarantee but
    still a working deploy" was not true for that app.
    """

    SPLIT_COMMENT = {'A_NOTE': '{# optional oidc wiring:',
                     'Z_END': '{{ oidc.issuer }} #}'}

    def _run(self, services, integrations=None):
        compose = {'services': {
            name: {'environment': dict(env)} for name, env in services.items()
        }}
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': integrations or {}},
        )
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def test_a_broken_service_does_not_un_pop_a_clean_one(self):
        compose, _ = self._run({
            'web': {'EMAIL_URL': 'smtp://{{ smtp.username }}@{{ smtp.host }}'},
            'worker': self.SPLIT_COMMENT,
        })
        self.assertEqual(compose['services']['web']['environment'], {})
        self.assertEqual(
            sorted(compose['services']['worker']['environment']),
            ['A_NOTE', 'Z_END'],
        )

    def test_the_malformed_url_does_not_come_back(self):
        compose, info = self._run({
            'web': {'EMAIL_URL': 'smtp://{{ smtp.username }}@{{ smtp.host }}'},
            'worker': self.SPLIT_COMMENT,
        })
        rendered = yaml.safe_load(
            Template(yaml.dump(compose)).render(**_compose_render_context(info)),
        )
        self.assertNotIn(
            'EMAIL_URL', rendered['services']['web']['environment'] or {},
        )

    def test_the_undo_handles_list_form_environments(self):
        # `environment:` may be a list of "KEY=value" strings, and the
        # undo has to re-apply a pop in that form too.
        compose = {'services': {'app': {'environment': [
            'EMAIL_URL=smtp://{{ smtp.username }}@{{ smtp.host }}',
            'A_NOTE={# optional oidc wiring:',
            'Z_END={{ oidc.issuer }} #}',
            'PLAIN=keepme',
            # Value MENTIONS a popped key's name. Matching on
            # containment rather than the key would take this too.
            'NOTE=see EMAIL_URL for details',
        ]}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        env = compose['services']['app']['environment']
        self.assertNotIn(
            'EMAIL_URL=smtp://{{ smtp.username }}@{{ smtp.host }}', env,
        )
        self.assertIn('A_NOTE={# optional oidc wiring:', env)
        self.assertIn('Z_END={{ oidc.issuer }} #}', env)
        self.assertIn('PLAIN=keepme', env)
        self.assertIn('NOTE=see EMAIL_URL for details', env)
        Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_the_undo_is_deterministic(self):
        # Same input, same answer -- the undo walks the document's own
        # key order rather than a set's.
        def once():
            compose = {'services': {
                'web': {'environment': {
                    'E': 'smtp://{{ smtp.username }}@{{ smtp.host }}',
                    'F': '{{ smtp.port }}', 'G': '{{ oidc.issuer }}',
                }},
                'worker': {'environment': dict(self.SPLIT_COMMENT)},
            }}
            _delete_unset_integration_env_keys(
                compose, _compute_integrations_context(
                    {'id': 'i1', 'integrations': {}}),
            )
            return {k: sorted(v['environment'])
                    for k, v in compose['services'].items()}
        first = once()
        for _ in range(5):
            self.assertEqual(once(), first)

    def test_a_clean_pop_in_the_SAME_service_still_survives(self):
        compose, _ = self._run({'app': dict(
            self.SPLIT_COMMENT, H='{{ oidc.issuer }}',
        )})
        env = compose['services']['app']['environment']
        self.assertNotIn('H', env)
        self.assertEqual(sorted(env), ['A_NOTE', 'Z_END'])


class UnhashableDestinationFieldsTests(TestCase):
    """Catalog metadata is JSON, so `container`/`key` can arrive as a list.

    Pass 1 used them as dict keys directly, and an unhashable value
    raised TypeError out of `/start/` -- inconsistent with every other
    shape guard in this module.
    """

    def test_an_unhashable_container_or_key_is_skipped(self):
        for dest in ({'type': 'oidc', 'container': ['a'], 'key': 'K'},
                     {'type': 'oidc', 'container': 'app', 'key': ['K']},
                     {'type': 'oidc', 'container': {'a': 1}, 'key': 'K'},
                     {'type': 'oidc', 'container': 'app', 'key': 7}):
            with self.subTest(dest=dest):
                compose = {'services': {'app': {'environment': {'K': 'v'}}}}
                _delete_unset_integration_env_keys(compose, {
                    'id': 'i1', 'integrations': {},
                    'configurations': [{'destinations': [dest]}],
                })
                self.assertEqual(
                    compose['services']['app']['environment'], {'K': 'v'},
                )


class ACallOnAnUnsetFieldRendersNothingTests(TestCase):
    """`_UnsetField.__call__` returning SELF is load-bearing.

    Returning `None` instead passes every other test in this file, and
    writes the literal string `None` into the compose as an env value:

        {{ oidc.host.upper() }}   self -> ''      None -> 'None'

    That is precisely the garbage-value class `__missing__` exists to
    prevent, so it needs a case whose result is rendered DIRECTLY. The
    docstring's usual example, `{{ smtp.from_address.split('@')[0] }}`,
    renders `''` either way and demonstrated nothing.
    """

    def _render(self, template):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        return Template(template).render(**_compose_render_context(info))

    def test_a_called_field_renders_empty_not_none(self):
        for template in ('{{ oidc.host.upper() }}',
                         '{{ oidc.host.strip().lower() }}',
                         '{{ oidc.a.b.c() }}'):
            with self.subTest(template=template):
                rendered = self._render(template)
                self.assertEqual(rendered, '')
                self.assertNotIn('None', rendered)

    def test_a_called_field_inside_a_larger_value_stays_empty(self):
        self.assertEqual(self._render('x={{ oidc.host.upper() }};'), 'x=;')


class DuplicateListFormKeysTests(TestCase):
    """A list-form `environment` may carry the same NAME twice.

    The undo used to diff by key name, so popping one occurrence left
    the name still present and the pop read as "nothing was popped". It
    was then never replayed and the referencing entry came back -- a
    silent under-pop rendering `EMAIL_URL=smtp://`, the malformed URL
    the strip exists to prevent, while the guard reported success.
    """

    DUPES = ['EMAIL_URL=static', 'EMAIL_URL=smtp://{{ smtp.host }}',
             'BROKE={% raw %}', 'B2={{ smtp.user }}{% endraw %}']

    def _run(self, env):
        compose = {'services': {'app': {'image': 'x', 'environment': list(env)}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def test_only_the_referencing_occurrence_is_popped(self):
        compose, _ = self._run(self.DUPES)
        env = compose['services']['app']['environment']
        self.assertIn('EMAIL_URL=static', env)
        self.assertNotIn('EMAIL_URL=smtp://{{ smtp.host }}', env)

    def test_the_malformed_url_is_not_rendered(self):
        compose, info = self._run(self.DUPES)
        rendered = yaml.safe_load(
            Template(yaml.dump(compose)).render(**_compose_render_context(info)))
        env = rendered['services']['app']['environment']
        self.assertNotIn('EMAIL_URL=smtp://', env)
        self.assertIn('EMAIL_URL=static', env)

    def test_duplicate_names_that_both_reference_are_both_popped(self):
        compose, _ = self._run(['E={{ smtp.host }}', 'E={{ smtp.port }}', 'KEEP=1'])
        self.assertEqual(compose['services']['app']['environment'], ['KEEP=1'])


class OneLevelMissIsQuietTests(TestCase):
    """Pins the shape Feature #3 must NOT lean on.

    The policy is "a configured integration fails loudly on a field it
    does not carry". That holds only for a CHAINED dereference. Reading
    the field directly renders `''`, exactly as a plain dict does, so a
    half-registered OIDC client deploys an empty secret rather than
    refusing. Asserted here so the docstrings cannot drift back into
    claiming a refusal that does not exist.
    """

    PARTIAL = {'oidc': {'issuer': 'https://id.example'}}

    def _render(self, template):
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': self.PARTIAL})
        return Template(template).render(**_compose_render_context(info))

    def test_a_direct_read_of_a_missing_field_is_quiet(self):
        self.assertEqual(self._render('{{ oidc.client_secret }}'), '')

    def test_a_chained_read_is_loud(self):
        for template in ('{{ oidc.client_secret.x }}',
                         '{{ oidc.client_secret|int }}'):
            with self.subTest(template=template):
                with self.assertRaises(UndefinedError):
                    self._render(template)

    def test_this_matches_a_plain_dict_exactly(self):
        # Why the quiet case is tolerated: it is not a regression, it is
        # Jinja's own behaviour on the raw blob.
        for template in ('{{ oidc.client_secret }}', '{{ oidc.issuer }}'):
            with self.subTest(template=template):
                self.assertEqual(
                    self._render(template),
                    Template(template).render(**self.PARTIAL),
                )


class ASymbolDefinedInOneValueAndUsedInAnotherTests(TestCase):
    """The guard must RENDER, not merely compile.

    A macro or namespace can be defined in one env value and used in
    another. The definition dereferences an unset type so it is popped;
    the use dereferences nothing so it is kept. What remains compiles
    perfectly and raises at render:

        A: '{% macro u(p) %}{{ oidc.issuer }}{{ p }}{% endmacro %}'
        B: '{{ u(1) }}'                 -> "'u' is undefined"

    That is a 500 at `/start/` on a compose that renders on `main`, and
    a compile-only guard cannot see it. It is also the counter-example
    to the claim that the binding covers every aliasing residual: the
    binding covers `oidc`, not the macro name bound to it.
    """

    CASES = {
        'macro': {'A_DEF': '{% macro u(p) %}{{ oidc.issuer }}{{ p }}{% endmacro %}',
                  'B_USE': '{{ u(1) }}'},
        'namespace': {'A_DEF': '{% set ns = namespace(v=smtp.host) %}',
                      'B_USE': '{{ ns.v }}'},
    }

    def _run(self, env):
        compose = {'services': {'app': {'environment': dict(env)}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def test_the_definition_is_not_popped_out_from_under_its_use(self):
        for name, env in self.CASES.items():
            with self.subTest(case=name):
                compose, info = self._run(env)
                # Must not raise: this is the 500 the guard now catches.
                Template(yaml.dump(compose)).render(
                    **_compose_render_context(info))

    def test_both_halves_survive(self):
        for name, env in self.CASES.items():
            with self.subTest(case=name):
                compose, _ = self._run(env)
                self.assertEqual(
                    compose['services']['app']['environment'], env)

    def test_compiling_alone_would_not_have_caught_it(self):
        # Pins the premise. The stripped document COMPILES; only
        # rendering it reveals the missing definition.
        stripped = {'services': {'app': {'environment': {'B_USE': '{{ u(1) }}'}}}}
        text = yaml.dump(stripped)
        Environment().from_string(text)  # compiles happily
        with self.assertRaises(UndefinedError):
            Environment().from_string(text).render()

    def test_a_clean_pop_beside_them_still_happens(self):
        # The guard must not become so timid that the feature stops
        # working: an unrelated clean pop in the same service survives.
        compose, _ = self._run(dict(self.CASES['macro'],
                                    H='{{ oidc.issuer }}'))
        self.assertNotIn('H', compose['services']['app']['environment'])


class TheGuardRendersAgainstItsOwnCopyTests(TestCase):
    """The guard must not let a catalog expression touch caller state.

    `_compose_render_context` copies only the top level, and the guard
    RENDERS. A side-effecting expression therefore ran against the
    caller's own nested objects, and what it did was permanent:

        {{ volumes.popitem() and '' }}

    emptied `greffon_info['volumes']`, which `create_volumes_then_copy_files`
    consumes immediately after `create_compose` returns -- so the
    instance came up with no volumes, or `create_compose` raised
    outright on the second call. The undo can repeat that render many
    times over.

    This module treats the catalog as hostile everywhere else; the guard
    renders against a deep copy so it is treated as hostile here too.
    """

    def _info(self):
        return _compute_integrations_context({
            'id': 'i1', 'integrations': {},
            'volumes': {'v1': {'value': 'a'}, 'v2': {'value': 'b'}},
            'networks': {'n1': {'value': 'n'}},
        })

    def test_a_mutating_expression_does_not_reach_the_caller(self):
        info = self._info()
        compose = {'services': {'app': {'environment': {
            'A': "{{ volumes.popitem() and '' }}",
        }}}}
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(
            info['volumes'], {'v1': {'value': 'a'}, 'v2': {'value': 'b'}})

    def test_a_nested_mutation_does_not_reach_the_caller(self):
        info = self._info()
        compose = {'services': {'app': {'environment': {
            'A': "{{ volumes.v1.clear() and '' }}",
        }}}}
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(info['volumes']['v1'], {'value': 'a'})

    def test_the_strip_still_works_beside_a_mutating_expression(self):
        info = self._info()
        compose = {'services': {'app': {'environment': {
            'A': "{{ volumes.popitem() and '' }}",
            'H': '{{ oidc.issuer }}',
        }}}}
        _delete_unset_integration_env_keys(compose, info)
        self.assertNotIn('H', compose['services']['app']['environment'])

    def test_an_uncopyable_context_skips_the_guard_rather_than_risking_it(self):
        info = self._info()
        info['unclonable'] = (i for i in [1])
        compose = {'services': {'app': {'environment': {'H': '{{ oidc.issuer }}'}}}}
        # Must not raise; the strip proceeds without the guard.
        _delete_unset_integration_env_keys(compose, info)
        self.assertNotIn('H', compose['services']['app']['environment'])


class TheContextIsCopiedPerRenderTests(TestCase):
    """Copying the guard's context ONCE was not enough.

    The guard renders several times: before the strip, after it, and
    once per undo trial. Sharing one copy across them means a catalog
    expression that mutates the context corrupts every later verdict,
    so the guard answers a different question than `create_compose`
    will ask with its own fresh context. Both directions shipped bugs.
    """

    def _create(self, gid, env):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'GREFFON_PATH': tmp}):
                info = {'id': gid, 'integrations': {}, 'name': 'n',
                        'volumes': {'v': {'value': 'vol1'}},
                        'networks': {}, 'ports': [], 'configurations': []}
                compose = {'services': {'app': {'environment': dict(env)}}}
                compose_module.create_compose(compose, info)
                written = pathlib.Path(tmp, gid, 'docker-compose.yml').read_text()
        return yaml.safe_load(written)['services']['app']['environment'] or {}

    def test_a_mutation_that_REMOVES_does_not_un_pop_everything(self):
        # `popitem()` empties the shared copy on the first render, so
        # every later render died on it and the guard undid every pop --
        # shipping glitchtip's `EMAIL_URL` as `smtp://:@`.
        env = self._create('i1', {
            'EMAIL_URL': 'smtp://{{ smtp.user }}@{{ smtp.host }}',
            'SELF': "{{ volumes.popitem() and '' }}",
        })
        self.assertNotIn('EMAIL_URL', env)

    def test_a_mutation_that_ADDS_does_not_get_the_guard_to_approve(self):
        # The mirror: the first render created `v2` in the shared copy,
        # so the guard approved a document whose real render raised.
        env = self._create('i2', {
            'AAA': '{{ volumes.setdefault("v2", volumes["v"]) and oidc.issuer }}',
            'ZZZ': '{{ volumes.v2.value }}',
        })
        self.assertEqual(env.get('ZZZ'), 'vol1')

    def test_an_ordinary_pop_is_unaffected(self):
        env = self._create('i3', {'H': '{{ oidc.issuer }}', 'X': '1'})
        self.assertEqual(env, {'X': '1'})

    def test_the_helper_does_not_mutate_the_context_it_is_given(self):
        context = {'volumes': {'v1': {'value': 'a'}, 'v2': {'value': 'b'}}}
        compose = {'services': {'app': {'environment': {
            'A': "{{ volumes.popitem() and '' }}"}}}}
        self.assertTrue(_document_renders(compose, context))
        self.assertEqual(len(context['volumes']), 2)

    def test_two_calls_with_the_same_context_agree(self):
        context = {'volumes': {'v1': {'value': 'a'}, 'v2': {'value': 'b'}}}
        compose = {'services': {'app': {'environment': {
            'A': "{{ volumes.popitem() and '' }}"}}}}
        first = _document_renders(compose, context)
        self.assertEqual(_document_renders(compose, context), first)


class DuplicateKeysReachingTheUndoTests(TestCase):
    """Duplicate list-form entries AND the undo path, together.

    Each half is covered on its own -- `DuplicateListFormKeysTests` for
    the multiset diff, `TheUndoIsBoundedTests` for the replay -- but
    nothing crossed them, and the multiset diff only matters on the undo
    path. Diffing by name instead would report the pop as never having
    happened, and restore the referencing entry.
    """

    def test_a_duplicate_name_survives_a_round_trip_through_the_undo(self):
        compose = {'services': {'app': {'environment': [
            'EMAIL_URL=static',
            'EMAIL_URL=smtp://{{ smtp.host }}',
            'A_OPEN={# note',                    # forces the undo
            'Z_END={{ oidc.issuer }} #}',
            'KEEP=1',
        ]}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        env = compose['services']['app']['environment']
        self.assertIn('EMAIL_URL=static', env)
        self.assertNotIn('EMAIL_URL=smtp://{{ smtp.host }}', env)
        self.assertIn('KEEP=1', env)
        Template(yaml.dump(compose)).render(**_compose_render_context(info))


class ItemsRemovedIsAMultisetDiffTests(TestCase):
    """`_items_removed` counts occurrences, not membership.

    Tested at the helper directly and deliberately. The strip cannot
    currently produce the distinguishing state -- both passes remove
    EVERY entry matching a key, so two identical entries always go
    together -- which means no end-to-end input separates a multiset
    diff from a plain `not in` comprehension today.

    That is an argument for pinning the contract here rather than
    pretending an integration-level test covers it: the undo replays
    whatever this returns, so if a future pass ever removes one of two
    identical entries, a membership test would report nothing removed
    and silently restore it.
    """

    def test_one_of_two_identical_entries_removed_is_reported(self):
        self.assertEqual(_items_removed(['a', 'a', 'b'], ['a', 'b']), ['a'])
        self.assertEqual(_items_removed(['a', 'b', 'a'], ['a']), ['b', 'a'])

    def test_both_removed_is_reported_twice(self):
        self.assertEqual(_items_removed(['a', 'a'], []), ['a', 'a'])

    def test_nothing_removed_is_empty(self):
        self.assertEqual(_items_removed(['x', 'y'], ['x', 'y']), [])

    def test_order_follows_the_document(self):
        self.assertEqual(_items_removed(['a', 'b', 'c'], ['b']), ['a', 'c'])

    def test_unhashable_items_are_handled(self):
        # Env values can be lists or dicts, which a Counter would refuse.
        before = [('K', ['x']), ('L', {'a': 1}), ('M', 'plain')]
        self.assertEqual(
            _items_removed(before, [('M', 'plain')]),
            [('K', ['x']), ('L', {'a': 1})],
        )


class DefaultDependsOnWhatItIsAppliedToTests(TestCase):
    """`|default` on the MAPPING is not the same as on a FIELD.

    Two comments in the module contradicted each other on this, and the
    wrong one was written as guidance for whoever builds the OIDC client
    registration. Pinned so neither can drift again.
    """

    def _render(self, template):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        return Template(template).render(**_compose_render_context(info))

    def test_default_on_the_mapping_does_not_fire(self):
        # The binding is a defined-but-empty mapping, so there is
        # nothing for `default` to replace.
        self.assertEqual(self._render('{{ oidc|default("d") }}'), '{}')

    def test_default_on_a_field_does_fire(self):
        # A missing field IS undefined.
        self.assertEqual(self._render('{{ oidc.issuer|default("d") }}'), 'd')

    def test_get_with_a_default_fires(self):
        self.assertEqual(self._render('{{ oidc.get("k", "d") }}'), 'd')

    def test_the_baked_file_path_agrees(self):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        self.assertEqual(
            _render_baked_file('{{ oidc|default("d") }}', info, 'f.json'), '{}')


class AGuardWhoseTestCannotEvaluateStillPopsTests(TestCase):
    """The exemption assumes a guard RENDERS when the type is unset.

    It assumes too much. `_UnsetField` absorbs attribute access and
    calls, not arithmetic or serialisation, so the test itself can
    raise:

        {% if smtp.port|int > 0 %}true{% else %}false{% endif %}

    That is unrecoverable, not merely wrong: the document fails to
    render BEFORE the strip, so the document guard takes no snapshot and
    the undo never runs -- a 500 at `/start/` with no fallback. Popping
    the key deploys cleanly.

    Not a regression against `main`, which raises identically. It is a
    hole in the exemption this feature added.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_test_that_raises_when_unset_is_popped(self):
        for value in (
            '{% if smtp.port|int > 0 %}true{% else %}false{% endif %}',
            '{% if smtp.port > 25 %}a{% endif %}',
            '{{ "a" if smtp.host|abs else "b" }}',
            '{% if smtp.host|tojson %}a{% endif %}',
            '{% if smtp.a.b|int %}a{% endif %}',
            '{% for x in [1] if smtp.port|int %}{{ x }}{% endfor %}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_the_whole_document_still_deploys(self):
        # The consequence: the neighbouring key survives instead of the
        # instance failing to start.
        compose = {'services': {'app': {'environment': {
            'SMTP_ENABLED': '{% if smtp.port|int > 0 %}true{% else %}false{% endif %}',
            'OTHER': 'ok',
        }}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['app']['environment'], {'OTHER': 'ok'})
        Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_a_guard_that_DOES_render_is_still_kept(self):
        # The probe must not become "pop every guard". `|default`
        # rescues the same expression, and that must still be kept.
        for value in (
            '{% if smtp.port|default(25)|int > 0 %}a{% else %}b{% endif %}',
            '{% if smtp %}on{% else %}off{% endif %}',
            '{% if smtp.host %}x{% else %}y{% endif %}',
            '{% if oidc.issuer.startswith("h") %}a{% endif %}',
        ):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))

    def test_the_probe_does_not_blame_the_type_for_other_context(self):
        # Everything but the integration is bound to a total stand-in,
        # so a value that needs real context is not popped for failing
        # in a probe that cannot supply it.
        self.assertTrue(self._survives(
            '{% if smtp %}{{ config.PORT|int }}{% else %}0{% endif %}'
            if False else
            '{% if smtp %}on{% else %}{{ config.PORT|int }}{% endif %}'))


class TheProbeOnlyJudgesTheIntegrationTests(TestCase):
    """The probe must not pop a value that never mentions the type.

    It binds every OTHER name to a stand-in, and the stand-in cannot do
    everything a real value can. `{{ config|tojson }}` fails only
    because the stand-in will not serialise -- the key was removed while
    the real render produced `{"X": "1"}`. A value the integration does
    not appear in cannot fail *because of* the integration.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_an_unrelated_operand_that_trips_the_stand_in_is_not_blamed(self):
        # A stand-in is not a real value. `{% if config|tojson and oidc %}`
        # fails the probe because the STAND-IN for `config` will not
        # serialise, not because of `oidc` -- and it renders `off`
        # against a real dict. A failure counts only if the same render
        # succeeds once the integration is populated.
        for value in ('{% if config|tojson and oidc %}on{% else %}off{% endif %}',
                      '{% if config|tojson and smtp.host %}a{% else %}b{% endif %}'):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))

    def test_a_comparison_does_not_hide_the_unset_field(self):
        # A stand-in answered comparisons out of its own truthiness, so
        # this guard took the same branch under every assignment and the
        # unset field was never reached -- while the real render, where
        # `instance_port` really is `''`, reaches it and raises. The
        # probe now uses the real value, so it takes the real branch.
        self.assertFalse(self._survives(
            "{% if instance_port == '' and oidc.port|int > 0 %}a{% else %}b{% endif %}",
            instance_port=''))

    def test_a_coercion_takes_the_real_branch(self):
        # `__int__`, `__float__`, `__len__` and friends each answered the
        # same way under every stand-in assignment, so a compound guard
        # short circuited at the coercion and never reached the unset
        # field. With the real `config` the coercion answers what the
        # render will answer, and the field is reached.
        for value in (
            '{% if config.PORT|int > 0 and oidc.port|int > 0 %}a{% else %}b{% endif %}',
            '{% if config.PORT|float > 0 and smtp.port|int %}a{% endif %}',
            '{% if config|length > 0 and oidc.port|int > 0 %}a{% else %}b{% endif %}',
            '{% if config|count > 0 and smtp.port|int %}a{% endif %}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(
                    value, config={'PORT': '8080', 'X': '1'}))

    def test_a_type_test_takes_the_real_branch(self):
        # `is mapping` is decided by the object's CONCRETE TYPE, which no
        # amount of truthiness-varying dunders could imitate: a stand-in
        # is not a dict, so this short circuited and the key was kept
        # for the real render to fail on.
        self.assertFalse(self._survives(
            '{% if config is mapping and oidc.port|int > 0 %}a{% else %}b{% endif %}',
            config={'PORT': '8080'}))

    def test_a_guard_that_fails_for_its_own_reasons_is_not_blamed(self):
        # `config.X|abs` raises on a string whatever the integration
        # does, so popping the key would not save the render. Only a
        # failure the populated integration fixes is the integration's.
        self.assertTrue(self._survives(
            '{% if config.X|abs > 0 and smtp.port|int %}a{% endif %}',
            config={'X': 'not-a-number'}))

    def test_an_indexed_field_is_still_attributed(self):
        # A configured field can be list-like, and a guard can index it
        # before the operation that fails. The populated stand-in did
        # not absorb indexing, so IT raised too, the failure looked
        # unattributable, and the key was kept for the render to fail on.
        for value in ('{% if oidc.values[0]|int > 0 %}a{% endif %}',
                      '{% if smtp.hosts[0].port|int %}a{% endif %}'):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_chained_field_is_still_attributed(self):
        # The populated side has to survive a CHAIN as well as a filter;
        # a plain number broke on `smtp.a.b` and made a real failure
        # look unattributable.
        self.assertFalse(self._survives('{% if smtp.a.b|int %}a{% endif %}'))
        self.assertFalse(self._survives('{% if smtp.host|tojson %}a{% endif %}'))

    def test_a_value_without_the_type_is_never_popped_by_the_probe(self):
        for value in ('{{ config|tojson }}', '{{ config|list }}',
                      '{{ config.X }}', '{{ instance_url|urlencode }}'):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))


class TheProbeUsesTheRealValueOfEveryOtherNameTests(TestCase):
    """A compound guard reaches the unset field only on one branch.

        {% if config and not feature and oidc.port|int > 0 %}

    Stand-ins forced a choice about each other name's truthiness, and
    whichever way it was made, some guard short circuited before the
    integration. Enumerating all 2^n assignments covered that, until
    the next review found a predicate the stand-in decided by type
    rather than by truth (`is mapping`) and the enumeration could not
    reach it at all.

    The real values are in the render context, so the probe takes the
    branch the render takes. Nothing to enumerate, and nothing to
    imitate.
    """

    def _survives(self, value, **names):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_guard_whose_real_branch_reaches_the_field_is_popped(self):
        for value, names in (
            ('{% if config and not feature and oidc.port|int > 0 %}a'
             '{% else %}b{% endif %}',
             {'config': {'PORT': '8080'}, 'feature': False}),
            ('{% if not config and feature and smtp.port|int %}a{% endif %}',
             {'config': {}, 'feature': True}),
            ('{% if config and oidc.port|int > 0 %}a{% else %}b{% endif %}',
             {'config': {'PORT': '8080'}}),
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value, **names))

    def test_the_same_guard_is_kept_when_the_real_branch_misses_it(self):
        # Not a blanket pop. Flip the operand that decides, and the
        # render never reaches the field, so the key ships.
        for value, names in (
            ('{% if config and not feature and oidc.port|int > 0 %}a'
             '{% else %}b{% endif %}',
             {'config': {'PORT': '8080'}, 'feature': True}),
            ('{% if not config and feature and smtp.port|int %}a{% endif %}',
             {'config': {'PORT': '8080'}, 'feature': True}),
        ):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value, **names))

    def test_a_guard_that_renders_either_way_is_kept(self):
        for value in ('{% if config and smtp %}a{% else %}b{% endif %}',
                      '{% if config and smtp.host %}a{% else %}b{% endif %}'):
            with self.subTest(value=value):
                self.assertTrue(
                    self._survives(value, config={'PORT': '8080'}))


class ANameDefinedByAnotherEnvValueTests(TestCase):
    """The whole compose is dumped and rendered as ONE template.

    A `{% set %}` in one env value therefore defines a name for every
    value after it. Probing a value in isolation left that name
    undefined, the guard short circuited, and the key was kept while
    the real document reached the unset field and raised. The
    definitions are collected once and prefixed onto each probe.
    """

    def _strip(self, env):
        compose = {'services': {'a': {'environment': dict(env)}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return compose['services']['a']['environment']

    def test_a_set_elsewhere_makes_the_guard_reachable(self):
        kept = self._strip({
            'A_SET': '{% set feature = true %}',
            'B_GUARD': '{% if feature and oidc.port|int > 0 %}x{% endif %}',
        })
        self.assertNotIn('B_GUARD', kept)
        self.assertIn('A_SET', kept)

    def test_without_the_set_the_guard_is_unreachable_and_kept(self):
        kept = self._strip({
            'B_GUARD': '{% if feature and oidc.port|int > 0 %}x{% endif %}',
        })
        self.assertIn('B_GUARD', kept)


class TheProbeDoesNotTouchDeploymentInputsTests(TestCase):
    """An exploratory render must not mutate the context it borrows.

    A guard can call a mutating method on another value --
    `{% if config.pop('X') and oidc %}` -- and the probe was popping
    from the very dict the real render then reads, so the second `pop`
    raised on a key that was already gone. Without the probe the single
    render succeeds, so this was damage the probe itself caused.
    """

    def _run(self, value, **names):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return info, 'K' in compose['services']['a']['environment']

    def test_a_mutating_guard_leaves_the_context_intact(self):
        info, kept = self._run(
            "{% if config.pop('X') and oidc %}a{% else %}b{% endif %}",
            config={'X': '1'})
        self.assertEqual(info['config'], {'X': '1'})
        # And the key survives: the real render pops once and succeeds.
        self.assertTrue(kept)


class ThePopulatedSideTriesMoreThanOneShapeTests(TestCase):
    """A configured field is not always a number.

    Every populated field had the concrete type `int`, so a guard doing
    anything else with it -- `{{ oidc.scopes + [] }}` on a list-valued
    field -- raised on the populated side too. The failure then looked
    unattributable, the key was kept, and the real unset render raised.

    The question this side asks is existential: is there a configured
    value that makes this render? So it tries a few shapes and takes
    any success.
    """

    def _survives(self, value, **names):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_one_guard_can_need_two_shapes_at_once(self):
        # `oidc.scopes + []` and `oidc.issuer + ''` in one expression:
        # every uniform shape fails one half, so the failure looked
        # unrelated and the key was kept.
        self.assertFalse(self._survives(
            "{% if [oidc.scopes + [], oidc.issuer + ''] %}x{% endif %}"))

    def test_a_list_valued_field_is_attributable(self):
        self.assertFalse(self._survives('{% if oidc.scopes + [] %}a{% endif %}'))

    def test_a_string_valued_field_is_attributable(self):
        self.assertFalse(
            self._survives('{% if smtp.host ~ "x" and smtp.port|int %}a{% endif %}'))

    def test_a_mapping_valued_field_is_attributable(self):
        self.assertFalse(
            self._survives('{% if oidc.claims["k"]|int > 0 %}a{% endif %}'))

    def test_a_value_no_shape_can_save_is_still_not_blamed(self):
        # `config.X|abs` raises on a string whatever the integration is,
        # so no populated shape rescues it and the key is kept.
        self.assertTrue(self._survives(
            '{% if config.X|abs > 0 and smtp.port|int %}a{% endif %}',
            config={'X': 'not-a-number'}))


class DefinednessIsNotSomethingTheProbeGuessesTests(TestCase):
    """A name the real render will not bind is left unbound in the probe.

    Giving every other name a stand-in made them all DEFINED, and
    definedness is testable:

        {% if feature is not defined and oidc.port|int > 0 %}

    short circuited in every truth assignment because `feature` was
    always bound, so the guard was never reached and the key was kept --
    while the real render, with no `feature`, reaches the unset field
    and raises `UndefinedError` out of `create_compose`. The pre-strip
    render fails too, so there is no snapshot and the undo cannot save
    it.

    The caller knows the answer exactly, so the probe is told rather
    than made to enumerate a third state per name.
    """

    def _survives(self, value, **names):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_an_unbound_name_reaches_the_unset_field(self):
        for value in (
            '{% if feature is not defined and oidc.port|int > 0 %}a'
            '{% else %}b{% endif %}',
            '{% if feature is undefined and smtp.port|int %}a{% endif %}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_the_same_guard_is_kept_once_the_name_is_bound(self):
        # Not a blanket pop: bound, the guard short circuits for real
        # too, and the key ships.
        self.assertTrue(self._survives(
            '{% if feature is not defined and oidc.port|int > 0 %}a'
            '{% else %}b{% endif %}', feature=True))

    def test_a_bound_name_can_be_what_reaches_the_field(self):
        self.assertFalse(self._survives(
            '{% if feature is defined and oidc.port|int > 0 %}a'
            '{% else %}b{% endif %}', feature=True))
        self.assertTrue(self._survives(
            '{% if feature is defined and oidc.port|int > 0 %}a'
            '{% else %}b{% endif %}'))


class AGuardCanLeakThroughACallArgumentTests(TestCase):
    """A guard test only DECIDES -- unless it hands the type to a call.

    The exemption assumes the mapping's contents cannot reach the output
    through a test. Passing them INTO a call breaks that, because the
    call can stash them somewhere the value prints afterwards. Every
    guard slot was reachable this way, and in the glitchtip shape the
    key shipped as `smtp://` -- present and malformed, with the document
    guard silent because the document renders fine.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_an_argument_leak_pops_from_every_guard_slot(self):
        for value in (
            '{% set l=[] %}{% if l.append(smtp.host) %}{% endif %}{{ l[0] }}',
            "{% set l=[] %}{{ 'a' if l.append(smtp.password) else '' }}"
            '{{ l|join }}',
            '{% set d={} %}{% for x in [1] if d.update(h=smtp.host) %}'
            '{% endfor %}{{ d.h }}',
            '{% set l=[] %}{% if l.append(smtp) %}{% endif %}{{ l[0].host }}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_the_glitchtip_shape_of_it_pops(self):
        self.assertFalse(self._survives(
            'smtp://{% set l=[] %}{% if l.append(smtp.host) %}{% endif %}'
            '{{ l[0] }}'))

    def test_the_type_as_RECEIVER_still_guards(self):
        # A method called ON the integration decides the branch and
        # nothing leaves the test. Treating every call as unsafe popped
        # these, which are ordinary catalog conditionals.
        for value in (
            '{% if oidc.issuer.startswith("https") %}a{% endif %}',
            '{% if smtp.host.upper() %}x{% else %}y{% endif %}',
            '{{ "a" if smtp.host.strip() else "b" }}',
        ):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))


class ASelfReferentialEnvValueDoesNotCrashTests(TestCase):
    """A YAML alias can make an env value contain itself.

        A: &a
          - '{{ smtp.host }}'
          - *a

    `safe_load` accepts it and the render handles it, but walking the
    value for template strings never terminates. `main` deploys such a
    compose, so raising here would turn a working deploy into a 500.
    """

    def test_a_recursive_alias_is_popped_rather_than_raising(self):
        doc = yaml.safe_load(
            "services:\n  app:\n    environment:\n      A: &a\n"
            "        - '{{ smtp.host }}'\n        - *a\n")
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(doc, info)
        self.assertEqual(doc['services']['app']['environment'], {})

    def test_a_very_deep_value_is_popped_rather_than_raising(self):
        deep = ['{{ smtp.host }}']
        for _ in range(3000):
            deep = [deep]
        compose = {'services': {'app': {'environment': {'A': deep}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertEqual(compose['services']['app']['environment'], {})


class DominationWasTriedAndWithdrawnTests(TestCase):
    """A read inside a branch that only runs when configured still pops.

        {% if smtp %}{{ smtp.host }}{% else %}localhost{% endif %}

    cannot reach `smtp.host` when the integration is unset, so keeping
    it would render the author's `localhost` instead of dropping the
    key. That was implemented and then withdrawn, and this class exists
    so the next person to propose it starts from the measurement rather
    than the idea.

    It saved ZERO keys across the real catalog: every value the catalog
    ships guards in the TEST position, which the slot rule already
    covers. It was the only path in the rule that could UNDER-pop, and
    it did in four consecutive review rounds -- an `elif` body (which
    runs when the guard is false), a `{% set %}` escaping its branch, a
    guard sharing the value with literal text
    (`smtp://{% if smtp %}..{% endif %}@h` -> `smtp://@h`), and the
    licence leaking into nested constructs. It cost ~165 lines.

    Popping these is what `main` does, so this is parity, not a
    regression.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_read_in_a_guarded_branch_still_pops(self):
        for value in (
            '{% if smtp %}{{ smtp.host }}{% else %}localhost{% endif %}',
            '{{ smtp.port if smtp else 25 }}',
            '{% if not smtp %}none{% else %}{{ smtp.host }}{% endif %}',
            '{% if smtp and other %}{{ smtp.host }}{% endif %}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_the_shapes_that_made_it_unsafe_pop_too(self):
        # The four failure modes that killed it, kept as regression
        # cover in case someone reinstates the idea.
        for value in (
            '{% if smtp %}A{% elif True %}smtp://{{ smtp.host }}{% endif %}',
            '{% if smtp %}{% set h = smtp.host %}{% endif %}smtp://{{ h }}/',
            'smtp://{% if smtp %}{{ smtp.user }}{% endif %}@mailhost',
            '{% if true %}https://{{ smtp.host if smtp else "" }}/path{% endif %}',
            '{{ "https://" + (oidc.issuer if oidc else "") + "/auth" }}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_guard_in_the_TEST_position_is_still_exempt(self):
        # What the catalog actually uses, and what the slot rule covers.
        for value, unset in (
            ('{% if smtp %}on{% else %}off{% endif %}', 'off'),
            ('{{ "true" if smtp else "false" }}', 'false'),
            ('{% if not smtp %}unset{% endif %}', 'unset'),
        ):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))
                info = _compute_integrations_context(
                    {'id': 'i1', 'integrations': {}})
                self.assertEqual(
                    Template(value).render(**_compose_render_context(info)),
                    unset)


class TheReadRuleIsCompleteByConstructionTests(TestCase):
    """The base question is Jinja's, not ours.

    Enumerating the constructs that read a variable was wrong ELEVEN
    times, each miss an under-pop that shipped a malformed value. The
    base rule is now `meta.find_undeclared_variables`, which reports
    every use in every construct, so no twelfth construct can slip past
    it. What is enumerated instead is the GUARD positions, and that
    inversion is what makes the failure direction safe: miss a guard
    slot and one env var is over-popped on an integration nobody
    configured.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_string_concatenation_is_a_read(self):
        # The eleventh route. `{{ smtp ~ "" }}` renders `{}` exactly as
        # the bare `{{ smtp }}` does, but it is a Concat node, so the
        # rule that matched a bare Output name did not see it.
        for value in ('{{ smtp ~ "" }}', '{{ "" ~ smtp }}',
                      'X{{ smtp ~ "y" }}Z'):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_filter_BLOCK_does_not_crash_the_strip(self):
        # `{% filter upper %}..{% endfilter %}` is a `Filter` node whose
        # operand slot is EMPTY, because its operand is the block body.
        # Walking that raised an uncaught AttributeError out of
        # `/start/` -- a 500 caused by the analysis, not the input.
        self.assertFalse(
            self._survives('{% filter upper %}{{ smtp.host }}{% endfilter %}'))
        self.assertTrue(
            self._survives('{% filter upper %}plain{% endfilter %}'))

    def test_a_locally_bound_name_is_not_our_integration(self):
        # Scoping comes free with the base rule, and every hand-written
        # version of this got it wrong: a loop variable, a `{% set %}`
        # or a macro parameter named `oidc` has nothing to do with the
        # integration, so the key must survive.
        for value in (
            '{% for oidc in [{"issuer": "l"}] %}{{ oidc.issuer }}{% endfor %}',
            '{% set oidc = {"issuer": "x"} %}{{ oidc.issuer }}',
            '{% macro m(oidc) %}{{ oidc.issuer }}{% endmacro %}{{ m({"issuer":"y"}) }}',
        ):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))

    def test_a_local_binding_elsewhere_does_not_excuse_a_global_read(self):
        # The mirror, and the case that makes the scoping load-bearing
        # rather than merely permissive.
        self.assertFalse(self._survives(
            '{% macro m(smtp) %}{{ smtp.host }}{% endmacro %}{{ smtp.port|int }}'))

    def test_the_guard_slots_are_every_one_jinja_has(self):
        # Derived, not recalled. `For.test` -- the `{% for x in xs if
        # cond %}` filter -- was missed, so the `{% for %}` spelling of
        # a guard was popped while the `{% if %}` spelling was kept.
        # If a Jinja upgrade adds another decide-only slot, this fails
        # rather than the strip silently discarding a working setting.
        expected = {(cls, 'test')
                    for cls in vars(nodes).values()
                    if isinstance(cls, type) and issubclass(cls, nodes.Node)
                    and 'test' in getattr(cls, 'fields', ())}
        expected.add((nodes.Test, 'node'))
        self.assertEqual(set(_GUARD_SLOTS), expected)

    def test_a_loop_filter_is_a_guard(self):
        # Renders the author's fallback when unset and the other branch
        # when configured, exactly like the `{% if %}` form beside it.
        loop = ("{% for x in ['off'] if not smtp %}{{ x }}{% endfor %}"
                "{% for x in ['on'] if smtp %}{{ x }}{% endfor %}")
        self.assertTrue(self._survives(loop))
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        self.assertEqual(Template(loop).render(**_compose_render_context(info)), 'off')
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': {'smtp': {'host': 'm'}}})
        self.assertEqual(Template(loop).render(**_compose_render_context(info)), 'on')

    def test_a_loop_filter_does_not_excuse_a_read_in_the_body(self):
        # `For.test` is a guard; `For.body` is a sibling, not a child of
        # it, so a read inside the loop still pops.
        self.assertFalse(self._survives(
            '{% for x in [1] if smtp %}{{ smtp.host }}{% endfor %}'))

    def test_a_guard_slot_is_the_only_exemption(self):
        # Guards render the CORRECT value unset, because the binding is
        # a falsy empty mapping.
        for value in ('{% if smtp %}on{% else %}off{% endif %}',
                      '{{ "y" if smtp.host else "n" }}',
                      '{% if smtp.tls_mode == "tls" %}a{% endif %}',
                      '{{ smtp is defined }}'):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))

    def test_the_base_rule_sees_every_construct_i_could_invent(self):
        # The completeness claim, exercised rather than asserted. Each
        # of these uses the type somewhere a hand-written scanner would
        # have to know about specifically; `find_undeclared_variables`
        # reports it without being told any of them exist.
        for value in (
            '{% set x %}{{ smtp.host }}{% endset %}{{ x }}',
            '{% block b %}{{ smtp.host }}{% endblock %}',
            '{% call f() %}{{ smtp.host }}{% endcall %}',
            '{% filter upper %}{{ smtp.host }}{% endfilter %}',
            '{% for i in [1] %}{% else %}{{ smtp.host }}{% endfor %}',
            '{% macro m() %}{{ smtp.host }}{% endmacro %}{{ m() }}',
            '{% if 1 %}{% set smtp = {} %}{% endif %}{{ smtp.host }}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_test_ARGUMENT_is_not_a_guard(self):
        # `Test.node` is the operand -- the thing being tested. An
        # argument is not that, so the type appearing there is a read.
        for value in ('{{ x is eq(smtp.host) }}', '{{ x is in(smtp) }}',
                      '{{ x is sameas(smtp) }}'):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_guardedness_does_not_leak_to_a_read_inside_the_branch(self):
        # The risk the inversion creates: guardedness is inherited by
        # child nodes, so a read whose ancestor is a guard could be
        # excused. It is not -- the flag is per OCCURRENCE, and each of
        # these has one outside a guard slot.
        for value in (
            '{{ (smtp if smtp else smtp).host }}',
            '{% set x = smtp if smtp else {} %}{{ x.host }}',
            '{{ smtp.host if 1 else 2 }}',
            '{{ (smtp if 1 else {}).host }}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_guard_does_not_launder_a_read_beside_it(self):
        # Guardedness applies to the OCCURRENCE, not the value: a read
        # elsewhere in the same value still pops. The scan must keep
        # going after a guarded occurrence rather than concluding from
        # it, and it must not depend on which occurrence it happens to
        # visit first -- so the read is placed both before and after
        # the guard.
        self.assertFalse(self._survives(
            '{% if smtp %}on{% endif %}{{ smtp|urlencode }}'))
        self.assertFalse(self._survives(
            '{{ smtp.host }}{% if smtp %}x{% endif %}'))
        self.assertFalse(self._survives(
            '{{ smtp|urlencode }}{{ "a" if smtp else "b" }}'))


class IteratingTheTypeIsADereferenceTests(TestCase):
    """`{% for k in smtp %}` reads the mapping, and produces no node the
    other rules look at.

    It is the shortest spelling of what `{% for k, v in smtp|items %}`
    does -- that one was popped and this one was not, which is an
    arbitrary line between two forms of one idiom. Like the filter case
    it renders correctly when the integration is configured, so a
    catalog author has every reason to ship it, and unset it left the
    key present-but-empty with no log line.
    """

    CONFIGURED = {'smtp': {'host': 'mail.ex', 'user': 'u'}}

    def _run(self, value, integrations=None):
        compose = {'services': {'app': {'environment': {'E': value}}}}
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': integrations or {}})
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def _rendered(self, compose, info):
        out = Template(yaml.dump(compose)).render(**_compose_render_context(info))
        return (yaml.safe_load(out)['services']['app']['environment'] or {})

    def test_bare_iteration_is_popped_when_unset(self):
        compose, _ = self._run('X{% for k in smtp %}{{ k }},{% endfor %}')
        self.assertEqual(compose['services']['app']['environment'], {})

    def test_the_same_value_works_when_configured(self):
        value = 'X{% for k in smtp %}{{ k }},{% endfor %}'
        compose, info = self._run(value, self.CONFIGURED)
        self.assertEqual(self._rendered(compose, info)['E'], 'Xhost,user,')

    def test_iterating_an_expression_that_reaches_the_type_also_pops(self):
        # Same reason the Getattr target is searched as a subtree: the
        # defensive spelling is the one a careful author reaches for.
        for value in ('{% for k in (smtp or {}) %}{{ k }}{% endfor %}',
                      '{% for k in [smtp][0] %}{{ k }}{% endfor %}',
                      '{% for k in (smtp|default({})) %}{{ k }}{% endfor %}'):
            with self.subTest(value=value):
                compose, _ = self._run(value)
                self.assertEqual(compose['services']['app']['environment'], {})

    def test_iterating_something_else_is_untouched(self):
        for value in ('{% for k in other %}{{ k }}{% endfor %}',
                      "{% for k in ['a'] %}{{ k }}{% endfor %}"):
            with self.subTest(value=value):
                compose, _ = self._run(value)
                self.assertIn('E', compose['services']['app']['environment'])

    def test_outputting_the_type_is_popped(self):
        # `{{ smtp }}` renders the literal `{}` unset, and a Python dict
        # repr that breaks the YAML when configured. Neither is usable.
        compose, _ = self._run('X{{ smtp }}')
        self.assertEqual(compose['services']['app']['environment'], {})

    def test_a_guard_is_not_a_read(self):
        # These render the CORRECT unset branch because the binding is a
        # falsy empty mapping, so popping them would throw away a
        # working setting.
        compose, info = self._run('{% if smtp %}on{% else %}off{% endif %}')
        self.assertIn('E', compose['services']['app']['environment'])
        self.assertEqual(self._rendered(compose, info)['E'], 'off')

    def test_a_guard_still_reads_correctly_when_configured(self):
        compose, info = self._run(
            '{% if smtp %}on{% else %}off{% endif %}', self.CONFIGURED)
        self.assertEqual(self._rendered(compose, info)['E'], 'on')


class WholeMappingFiltersAreDereferencesTests(TestCase):
    """The sharpest case in this module, so it gets its own class.

    `{{ smtp|urlencode }}` and `{% for k, v in smtp|items %}` read the
    mapping's CONTENTS without naming any attribute, so neither a
    `Getattr` nor an attribute-naming filter appears and the scan saw
    nothing to pop.

    What makes them worse than the earlier misses is that they WORK.
    With SMTP configured they render correctly, so a catalog author has
    every reason to ship one -- and unset, the same value renders
    `smtp://@mailhost:25`, a malformed URL the app parses at boot. The
    strip stayed silent throughout: nothing was popped, so the document
    guard had nothing to undo and no log line was emitted.
    """

    CONFIGURED = {'smtp': {'host': 'mail.ex', 'user': 'u', 'password': 'p'}}

    def _run(self, value, integrations=None):
        compose = {'services': {'app': {'environment': {'E': value}}}}
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': integrations or {}})
        _delete_unset_integration_env_keys(compose, info)
        return compose, info

    def test_a_whole_mapping_filter_is_popped_when_unset(self):
        for value in ('smtp://{{ smtp|urlencode }}@mailhost:25',
                      '{% for k, v in smtp|items %}{{ k }}={{ v }};{% endfor %}',
                      '{{ smtp|dictsort }}',
                      '{{ smtp|xmlattr }}'):
            with self.subTest(value=value):
                compose, _ = self._run(value)
                self.assertEqual(compose['services']['app']['environment'], {})

    def test_the_same_value_still_works_when_configured(self):
        # The reason it must be popped rather than left alone: this is a
        # real, working idiom, not a broken one.
        value = 'smtp://{{ smtp|urlencode }}@mailhost:25'
        compose, info = self._run(value, self.CONFIGURED)
        rendered = Template(yaml.dump(compose)).render(
            **_compose_render_context(info))
        got = yaml.safe_load(rendered)['services']['app']['environment']['E']
        self.assertIn('host=mail.ex', got)
        self.assertNotIn('smtp://@', got)

    def test_the_malformed_url_is_gone_when_unset(self):
        value = 'smtp://{{ smtp|urlencode }}@mailhost:25'
        compose, info = self._run(value)
        rendered = Template(yaml.dump(compose)).render(
            **_compose_render_context(info))
        self.assertNotIn('smtp://@', rendered)

    def test_map_attr_does_not_smuggle_the_attribute_rule_through(self):
        # `map`'s positional argument names a FILTER -- but that filter
        # can be `attr`, which is the one indirection that turned the
        # old attribute rule back off.
        compose, _ = self._run("smtp://{{ [smtp]|map('attr','user')|join }}@h")
        self.assertEqual(compose['services']['app']['environment'], {})


class TheAcceptedOverPopTests(TestCase):
    """Where the subtree search pops a value that would have worked.

    Searching the whole Getattr target means a value that merely
    MENTIONS the name inside that target is popped, even when the name
    cannot be what the expression evaluates to:

        {{ (alpha if smtp else alpha).name }}

    reads `alpha` either way, so `main` renders it and this pops it.
    That is the deliberate trade -- over-pop costs one env var on an
    integration nobody configured, under-pop ships a malformed value --
    but it is a real cost, so it is asserted here rather than left in
    prose. Measured at ~250 extra pops per 3000 adversarial fragments
    and ZERO on the 120 real catalog values.

    If someone narrows the rule so these survive, this test should fail
    and be re-examined, not deleted unread.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_the_name_in_a_target_it_cannot_be_is_still_popped(self):
        # The name appears in the Getattr target but cannot be what the
        # expression evaluates to, so `main` renders these and this
        # drops them. Over-pop costs one env var on an integration
        # nobody configured; under-pop ships a malformed value.
        for value in ('{{ (alpha, smtp)[0].name }}',
                      '{{ {"a": alpha, "b": smtp}["a"].name }}',
                      '{{ ([alpha] + [smtp])[0].name }}'):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_ternary_TEST_is_a_guard_and_is_kept(self):
        # `{{ (alpha if smtp else alpha).name }}` used to be in the list
        # above. It is a guard -- the mapping decides which branch, its
        # contents never reach the output -- so keeping it renders the
        # correct value in both worlds, and the over-pop is gone.
        self.assertTrue(self._survives('{{ (alpha if smtp else alpha).name }}'))

    def test_a_value_that_never_mentions_the_name_is_untouched(self):
        # The boundary: over-popping requires the name to appear. It is
        # not a licence to pop anything with a Getattr in it.
        for value in ('{{ alpha.name }}', '{{ (alpha or beta).name }}',
                      '{{ instance_url }}', '{{ config.ANY }}'):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))


class GetOnAnUnsetIntegrationTests(TestCase):
    """`.get()` must not hand Jinja a bare `None`.

    `_UnsetIntegration` is a real `dict`, so `dict.get` returned `None`
    for a missing key and Jinja rendered the literal string `None` into
    a container's environment -- `smtp://None` -- which is neither empty
    nor correct, and is the class of garbage this object exists to
    prevent. A plain `{}` does the same, so this is an improvement on
    `main` rather than a regression from it.

    An explicit default still wins, because that is the caller saying
    what they want.
    """

    def _render(self, template):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        return Template(template).render(**_compose_render_context(info))

    def test_a_missing_key_with_no_default_renders_empty(self):
        # Via an alias, so the strip does not pop the value first.
        self.assertEqual(
            self._render('{% set s = smtp %}smtp://{{ s.get("host") }}'),
            'smtp://',
        )

    def test_an_explicit_default_still_wins(self):
        self.assertEqual(
            self._render('{% set s = smtp %}{{ s.get("host", "fallback") }}'),
            'fallback',
        )

    def test_a_present_key_is_returned(self):
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': {'smtp': {'host': 'mail.x'}}})
        ctx = _compose_render_context(info)
        self.assertEqual(
            Template('{% set s = smtp %}{{ s.get("host") }}').render(**ctx),
            'mail.x',
        )

    def test_it_is_still_a_dict(self):
        # The binding must stay dict-shaped for everything else.
        self.assertEqual(self._render('{% set s = smtp %}{{ s|tojson }}'), '{}')


class TheUndoIsBoundedTests(TestCase):
    """The undo is chunked and bounded by WALL CLOCK, and degrades safely.

    Each attempt costs a full dump+compile+render, and what a render
    costs is catalog-controlled and unbounded (`{{ range(2e7)|sum }}`),
    so capping the number of attempts capped the wrong thing. Keys are
    tried in batches and only a failing batch is split.

    On exhaustion the decisions already made are KEPT -- restoring
    everything was the earlier behaviour and it brought back glitchtip's
    `EMAIL_URL` rendering `smtp://:@`.

    The clock is faked rather than slept on, so these assert behaviour
    and cannot go red because the machine is busy.
    """

    def _compose(self, n):
        # Order matters: the strip decides in document order, so
        # EMAIL_URL is decided before the poisoned pair at the end.
        env = {'A_EMAIL_URL': 'smtp://{{ smtp.username }}@{{ smtp.host }}'}
        env.update({f'K{i:04d}': '{{ oidc.issuer }}' for i in range(n)})
        env['Y_OPEN'] = '{# note'
        env['Z_CLOSE'] = '{{ oidc.host }} #}'
        return {'services': {'app': {'environment': env}}}

    def _run(self, compose, clock_step=None):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        if clock_step is None:
            _delete_unset_integration_env_keys(compose, info)
        else:
            ticks = itertools.count(0.0, clock_step)
            fake = SimpleNamespace(monotonic=lambda: next(ticks))
            with patch.object(compose_module, 'time', fake):
                _delete_unset_integration_env_keys(compose, info)
        return compose['services']['app']['environment'], info

    def _renders(self, compose, info):
        return Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_a_small_case_is_decided_selectively(self):
        compose = self._compose(5)
        env, info = self._run(compose)
        self.assertNotIn('A_EMAIL_URL', env)   # clean pops survive
        self.assertNotIn('K0000', env)
        self.assertIn('Y_OPEN', env)           # the straddling pair is undone
        self._renders(compose, info)

    def test_a_batch_that_renders_is_kept_whole(self):
        compose = self._compose(_UNDO_CHUNK * 3)
        env, _ = self._run(compose)
        self.assertNotIn('A_EMAIL_URL', env)
        self.assertFalse([k for k in env if k.startswith('K')])

    def test_decisions_made_before_the_budget_ran_out_are_kept(self):
        # The regression guard. A clock that runs out partway must not
        # resurrect the malformed URL decided in the first batch.
        compose = self._compose(_UNDO_CHUNK * 6)
        env, _ = self._run(compose, clock_step=_UNDO_TIME_BUDGET / 4)
        self.assertNotIn('A_EMAIL_URL', env)

    def test_running_out_of_budget_still_leaves_a_renderable_document(self):
        compose = self._compose(_UNDO_CHUNK * 6)
        _, info = self._run(compose, clock_step=_UNDO_TIME_BUDGET / 4)
        self._renders(compose, info)

    def test_with_no_budget_at_all_the_document_still_renders(self):
        # Nothing is decided, so nothing is popped -- safe, not correct.
        # The invariant that must never break is that it renders.
        compose = self._compose(_UNDO_CHUNK)
        env, info = self._run(compose, clock_step=_UNDO_TIME_BUDGET)
        self.assertIn('Y_OPEN', env)
        self._renders(compose, info)

    def test_the_remainder_is_tried_once_more_as_a_whole(self):
        # When the poison was already decided, everything still
        # outstanding is fine together, so one last attempt recovers it
        # rather than shipping empty values for the tail.
        env = {'Y_OPEN': '{# note', 'Z_CLOSE': '{{ oidc.host }} #}'}
        env.update({f'K{i:04d}': '{{ oidc.issuer }}' for i in range(40)})
        compose = {'services': {'app': {'environment': env}}}
        got, info = self._run(compose, clock_step=_UNDO_TIME_BUDGET / 8)
        self.assertFalse([k for k in got if k.startswith('K')],
                         'the outstanding clean pops should be recovered')
        self._renders(compose, info)

    # --- the accepted residual, asserted rather than left implicit ---

    def _poison_in_the_tail(self):
        env = {f'K{i:04d}': '{{ oidc.issuer }}' for i in range(30)}
        env['Y_OPEN'] = '{# note'                       # poison, late
        env['ZA_EMAIL'] = 'smtp://{{ smtp.username }}@{{ smtp.host }}'
        env['ZZ_CLOSE'] = '{{ oidc.host }} #}'
        return {'services': {'app': {'environment': env}}}

    def test_the_budget_running_out_can_leave_a_must_pop_key_behind(self):
        # THE RESIDUAL, pinned on purpose. When the key that breaks the
        # document is itself in the undecided tail, the all-at-once
        # retry fails and every key in the tail is restored -- must-pop
        # ones included. So `ZA_EMAIL` survives and renders `smtp://@`.
        #
        # Reserving part of the budget for this phase was tried and
        # measured WORSE (it kept the key in the one case that
        # distinguished the two). Splitting a fixed budget cannot buy
        # work that is not there, so this is documented and made loud
        # rather than papered over.
        #
        # If someone later makes this pass, the residual is gone and
        # this test should be deleted, not adjusted.
        compose = self._poison_in_the_tail()
        got, info = self._run(compose, clock_step=_UNDO_TIME_BUDGET / 8)
        self.assertIn('ZA_EMAIL', got)
        self._renders(compose, info)   # still renders: safety holds

    def test_the_residual_is_reported_loudly_and_by_name(self):
        # An operator's only signal that an instance may be
        # misconfigured, so it has to be ERROR and it has to name keys.
        compose = self._poison_in_the_tail()
        with self.assertLogs('apps.utils.docker.compose', level='ERROR') as caught:
            self._run(compose, clock_step=_UNDO_TIME_BUDGET / 8)
        line = ''.join(caught.output)
        self.assertIn('app.ZA_EMAIL', line)
        self.assertIn('misconfigured', line)

    def test_the_clock_is_checked_inside_a_split_chunk_too(self):
        # A failing chunk is split and decided item by item. Without a
        # clock check INSIDE that loop, an expired budget still pays for
        # a whole chunk of renders -- up to 7 more, each unbounded in
        # cost. Poison the FIRST chunk so the split happens immediately,
        # and expire the clock as the inner loop starts.
        env = {'Y_OPEN': '{# note', 'Z_CLOSE': '{{ oidc.host }} #}'}
        env.update({f'K{i:04d}': '{{ oidc.issuer }}' for i in range(6)})
        expected = sorted(env)  # `env` itself is mutated in place below
        compose = {'services': {'app': {'environment': env}}}
        got, info = self._run(compose, clock_step=_UNDO_TIME_BUDGET / 2)
        # Nothing from that chunk may be decided individually.
        self.assertEqual(sorted(got), expected)
        self._renders(compose, info)

    def test_the_log_says_which_keys_were_kept_and_which_undone(self):
        # The undo's log is the operator's only record of which env vars
        # were dropped, so the labels have to be the right way round.
        env = {'A_POISON': '{# note', 'Z_CLOSE': '{{ oidc.host }} #}'}
        env.update({f'K{i}': '{{ oidc.issuer }}' for i in range(3)})
        compose = {'services': {'app': {'environment': env}}}
        with self.assertLogs('apps.utils.docker.compose', level='WARNING') as caught:
            self._run(compose)
        line = ''.join(caught.output)
        kept = line.split('kept ', 1)[1].split(' and undid ')[0]
        undone = line.split(' and undid ', 1)[1]
        for i in range(3):
            self.assertIn(f'app.K{i}', kept)
            self.assertNotIn(f'app.K{i}', undone)
        self.assertIn('app.Z_CLOSE', undone)
        self.assertNotIn('app.Z_CLOSE', kept)

    def test_the_shipped_budget_is_sane(self):
        # The tests patch this constant, so nothing otherwise pins the
        # value that actually ships. It has to be finite and small
        # enough that a stuck undo cannot hold `/start/` for minutes.
        self.assertGreater(_UNDO_TIME_BUDGET, 0)
        self.assertLessEqual(_UNDO_TIME_BUDGET, 30)
        self.assertGreaterEqual(_UNDO_CHUNK, 2)

    def test_exhausting_the_budget_is_logged(self):
        compose = self._compose(_UNDO_CHUNK * 6)
        with self.assertLogs('apps.utils.docker.compose', level='WARNING') as caught:
            self._run(compose, clock_step=_UNDO_TIME_BUDGET / 4)
        self.assertIn('undo budget', ''.join(caught.output))


class LogRotationIsInjectedBeforeTheStripTests(TestCase):
    """Ordering inside `create_compose`, which nothing pinned.

    The guard approves the exact text that will be rendered. Injecting
    the `logging:` block AFTERWARDS changed that text, and appending is
    not neutral to Jinja: an unclosed `{% raw %}` is accepted at EOF and
    rejected once anything follows it. So the guard could approve a
    document that then failed to compile.
    """

    def test_appending_content_can_change_jinja_s_answer(self):
        # The premise. Without this the ordering is arbitrary.
        Environment().from_string('x{% raw %}')
        with self.assertRaises(TemplateSyntaxError):
            Environment().from_string('x{% raw %}\nmore: content\n')

    def test_create_compose_injects_logging_before_stripping(self):
        order = []
        real_inject = compose_module._inject_instance_log_rotation
        real_strip = compose_module._delete_unset_integration_env_keys

        def inject(compose):
            order.append('inject')
            return real_inject(compose)

        def strip(compose, greffon_info):
            order.append('strip')
            svc = (compose.get('services') or {}).get('app') or {}
            order.append('logging' if 'logging' in svc else 'no-logging')
            return real_strip(compose, greffon_info)

        info = {'id': 'i1', 'integrations': {},
                'volumes': {}, 'networks': {}, 'ports': [], 'configurations': []}
        compose = {'services': {'app': {'image': 'nginx',
                                        'environment': {'H': '{{ oidc.a }}'}}}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'GREFFON_PATH': tmp}), \
                patch.object(compose_module, '_inject_instance_log_rotation', inject), \
                patch.object(
                    compose_module, '_delete_unset_integration_env_keys', strip):
            compose_module.create_compose(compose, info)

        self.assertEqual(order, ['inject', 'strip', 'logging'])


class MalformedPayloadShapesDoNotCrashTests(TestCase):
    """A malformed manager payload must not 500 a deploy that works.

    `_compute_config_context` documents defending exactly this; these two
    did not, and a non-mapping `integrations` or a non-list
    `configurations` raised straight out of `/start/`.
    """

    def test_a_non_mapping_integrations_is_treated_as_none(self):
        # Truthy non-dicts matter as much as falsy ones: `or {}` instead
        # of an isinstance check passes a list straight through, and the
        # `.get(t)` on it raises out of `/start/`.
        for bad in ('garbage', 7, [], ['smtp'], [1], {1, 2}, None, True):
            with self.subTest(bad=bad):
                info = _compute_integrations_context({'id': 'i1', 'integrations': bad})
                self.assertEqual(info['smtp'], {})
                self.assertEqual(info['oidc'], {})

    def test_a_non_mapping_integrations_still_pops(self):
        compose = {'services': {'app': {'environment': {'H': '{{ smtp.host }}'}}}}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': 'garbage'},
        )
        self.assertEqual(compose['services']['app']['environment'], {})

    def test_a_malformed_configurations_block_is_skipped(self):
        for bad in ('garbage', 7, None, [None, 'x', 7],
                    [{'destinations': 'nope'}], [{'destinations': None}]):
            with self.subTest(bad=bad):
                compose = {'services': {'app': {'environment': {'K': 'v'}}}}
                _delete_unset_integration_env_keys(compose, {
                    'id': 'i1', 'integrations': {}, 'configurations': bad,
                })
                self.assertEqual(
                    compose['services']['app']['environment'], {'K': 'v'},
                )


class AFalsyPresetIsBoundTests(TestCase):
    """A falsy preset on a top-level integration key is treated as unset.

    `_compute_integrations_context` uses `setdefault`, so a `greffon_info`
    already carrying `oidc: None` is never normalised to `{}`. Binding it
    is the safe direction: `None` would raise on `{{ oidc.a.b }}` and 500
    the start, while a real configured blob is truthy and still wins.
    """

    def _ctx(self, preset):
        info = dict(preset, id='i1', integrations={})
        return _compose_render_context(_compute_integrations_context(info))

    def test_a_falsy_preset_is_bound(self):
        for preset in ({'oidc': None}, {'oidc': []}, {'oidc': ''}, {'oidc': {}}):
            with self.subTest(preset=preset):
                self.assertIsInstance(self._ctx(preset)['oidc'], _UnsetIntegration)

    def test_a_falsy_preset_renders_instead_of_raising(self):
        self.assertEqual(
            Template('{{ oidc.a.b }}').render(**self._ctx({'oidc': None})), '',
        )

    def test_a_truthy_preset_still_wins(self):
        ctx = self._ctx({'oidc': {'issuer': 'https://preset'}})
        self.assertNotIsInstance(ctx['oidc'], _UnsetIntegration)
        self.assertEqual(ctx['oidc'], {'issuer': 'https://preset'})


class AcceptedResidualTests(TestCase):
    """The one case where this is worse than `main`, asserted on purpose.

    Keeping a value main popped exposes it to the pre-existing
    dump-then-template hazard: `yaml.dump` doubles an inner `'`, so the
    expression reaches Jinja malformed and fails the WHOLE render.

    Written down as a test rather than a comment so that if someone later
    fixes the root cause -- rendering before dumping -- this fails and
    tells them the residual is gone, instead of the knowledge living only
    in a paragraph nobody re-reads.
    """

    def test_a_quoted_type_token_inside_a_literal_is_kept_and_then_fails(self):
        value = "{{ instance_host|default('smtp.acme.com') }}"
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        # Kept: the token is inside a string literal, so it is not a
        # reference, and not popping it is the false-positive fix working.
        self.assertIn('K', compose['services']['a']['environment'])
        # And then the dump mangles it. `main` pops this key by accident
        # and deploys without it.
        ctx = _compose_render_context(info)
        ctx['instance_host'] = 'h'
        with self.assertRaises(TemplateSyntaxError):
            Template(yaml.dump(compose)).render(**ctx)

    def test_the_same_shape_without_a_type_token_breaks_main_too(self):
        # Shows the hazard is not ours: nothing here mentions an
        # integration, so both implementations keep it and both fail.
        value = "{{ instance_host|default('example.com') }}"
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertIn('K', compose['services']['a']['environment'])
        ctx = _compose_render_context(info)
        ctx['instance_host'] = 'h'
        with self.assertRaises(TemplateSyntaxError):
            Template(yaml.dump(compose)).render(**ctx)


class ParserFailureIsTreatedAsAReferenceTests(TestCase):
    """Pass 2 parses; both ways parsing can fail must POP, not escape.

    The direction matters. Over-popping loses one env var on an
    integration that is not configured anyway. Under-popping lets the
    reference reach the render, where it raises and `create_compose`
    500s -- the instance does not start at all.
    """

    def _pop(self, value):
        compose = {'services': {'s': {'environment': {'K': value}}}}
        _delete_unset_integration_env_keys(
            compose, {'id': 'i1', 'integrations': {'smtp': {'host': 'h'}}},
        )
        return 'K' not in compose['services']['s']['environment']

    def test_a_syntax_error_pops(self):
        # `yaml.dump` doubles an inner quote, so malformed expressions do
        # reach this function in practice.
        self.assertTrue(self._pop("{{ oidc.issuer''x }}"))

    def test_nesting_past_the_parser_stack_limit_pops(self):
        # `parse()` recurses per nesting level and blows the Python stack
        # well before Jinja reports anything. RecursionError is NOT a
        # TemplateError, so without its own guard it escapes as a 500 on
        # catalog-controlled input.
        deep = '{{ ' + '(' * 5000 + 'oidc.issuer' + ')' * 5000 + ' }}'
        self.assertTrue(self._pop(deep))

    def test_the_deep_input_really_does_blow_the_stack(self):
        # Pins the premise: if a future Jinja parses this iteratively the
        # test above stops covering anything and this says so.
        from jinja2 import Environment
        deep = '{{ ' + '(' * 5000 + 'oidc.issuer' + ')' * 5000 + ' }}'
        with self.assertRaises(RecursionError):
            Environment().parse(deep)


class ConstructScanningTests(TestCase):
    """What counts as a reference to an unset type.

    The rule: a construct that DEREFERENCES the type pops the key; one
    that merely mentions it does not. Survival is then made safe by the
    binding rather than by the scan being complete. Each case below
    is a way of getting exactly one of those two halves wrong.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_statement_blocks_are_scanned(self):
        # Statement blocks RENDER, so a reference in one is as fatal as a
        # reference in `{{ }}`. Scanning only `{{ }}` bodies let
        # `{% if oidc.issuer.startswith(...) %}` through to raise
        # UndefinedError at render -- the failure this feature exists to
        # prevent, reintroduced by the fix for the false positive.
        for value in (
            '{% set x = oidc.issuer %}{{ x }}',
            '{% for s in oidc.scopes %}{{ s }}{% endfor %}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_statement_that_only_GUARDS_is_kept(self):
        # `{% if %}` decides a branch; the mapping's contents never
        # reach the output through its test. Unset renders '' and
        # configured renders the branch, so both are correct and
        # popping would discard a working conditional.
        self.assertTrue(
            self._survives('{% if oidc.issuer.startswith("https") %}a{% endif %}'))

    def test_statement_blocks_are_scanned_for_smtp_too(self):
        # Not an oidc-only rule; the same hole existed for the type that
        # was already shipping.
        self.assertTrue(self._survives('{% if smtp.host %}x{% endif %}'))

    def test_token_inside_a_string_literal_is_data_not_a_reference(self):
        # `oidc.acme.com` here is a quoted hostname being concatenated,
        # not an attribute access. Popping it is the same false positive
        # as the whole-string match, just spelled inside the expression.
        self.assertTrue(self._survives('{{ "https://oidc.acme.com/realms/" ~ instance_id }}'))

    def test_comments_and_raw_blocks_are_not_evaluated(self):
        # `{# #}` is never rendered, and `{% raw %}` exists precisely to
        # ship a literal `{{ oidc.issuer }}` to a downstream config.
        self.assertTrue(self._survives('{# {{ oidc.issuer }} #}static'))
        self.assertTrue(self._survives('{% raw %}{{ oidc.issuer }}{% endraw %}'))

    def test_unbalanced_construct_is_popped_rather_than_left_to_explode(self):
        # A malformed template either way, but leaving it lets
        # `Template(yaml.dump(compose))` raise TemplateSyntaxError and
        # fail the WHOLE start; popping costs one env var.
        self.assertFalse(self._survives('{{ oidc.issuer }'))

    def test_closing_braces_inside_a_quoted_key_still_match(self):
        # `{{ oidc['a}}b'] }}` is a genuine reference whose body the
        # non-greedy match truncates at `oidc['`; that still matches.
        self.assertFalse(self._survives("{{ oidc['a}}b'] }}"))


class DelimiterInsideStringLiteralTests(TestCase):
    """A `}}` or `%}` inside a quoted literal must not end the construct.

    This is the failure the non-greedy regex had: the body stopped at the
    first closing delimiter wherever it appeared, so everything after one
    that lived inside a string was scanned by nothing at all, survived the
    pop, and raised at render.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_reference_after_a_quoted_closing_delimiter_is_found(self):
        self.assertFalse(self._survives('{{ "}}" ~ oidc.issuer.split("@")[0] }}'))

    def test_reference_after_a_quoted_block_delimiter_is_found(self):
        self.assertTrue(
            self._survives('{% if "%}" and oidc.issuer.startswith("h") %}y{% endif %}'),
        )

    def test_quoted_comment_delimiters_do_not_hide_a_reference(self):
        # A `{#` and a `#}` in two SEPARATE literals must not cause the
        # span between them -- which holds a real reference -- to be
        # treated as a comment.
        self.assertFalse(
            self._survives('{{ "{#" }}{{ oidc.issuer.startswith("h") }}{{ "#}" }}'),
        )

    def test_escaped_quote_does_not_end_the_literal(self):
        # The escape must HIDE a closing delimiter, or the assertion
        # holds with escape handling removed entirely. The first version
        # of this test used `{{ "a\\"b" ~ oidc.issuer }}`, where the
        # escaped quote hides nothing, and survived that mutation.
        self.assertFalse(self._survives('{{ "a\\"}}b" ~ oidc.issuer }}'))

    def test_a_quoted_delimiter_alone_still_does_not_pop(self):
        # The other direction: the literal `oidc.` host here is outside
        # any construct, so the key must survive.
        self.assertTrue(self._survives('{{ "}}" }} https://oidc.acme.com/x'))

    def test_raw_region_ends_at_the_tag_not_the_bare_word(self):
        # Raw content that merely CONTAINS the word `endraw` (followed by
        # any `%}`) must not close the region early. This value renders
        # as literal text, so dropping its key is a pure false positive.
        self.assertTrue(
            self._survives('{% raw %}literal endraw %} {{ oidc.issuer }}{% endraw %}'),
        )
        self.assertTrue(
            self._survives('{% raw %}100%} of {{ oidc.issuer }}{% endraw %}'),
        )

    def test_a_reference_after_a_closed_raw_region_is_still_found(self):
        # The other direction: once the region really is closed, what
        # follows evaluates and must be scanned.
        self.assertFalse(
            self._survives('{% raw %}{{ oidc.issuer }}{% endraw %}{{ oidc.issuer }}'),
        )

    def test_whitespace_control_markers_are_handled(self):
        self.assertFalse(self._survives('{{- oidc.issuer -}}'))
        self.assertTrue(self._survives('{%- if oidc.issuer -%}a{%- endif -%}'))
        self.assertTrue(self._survives('{%- raw -%}{{ oidc.issuer }}{%- endraw -%}'))


class NotOurVariableTests(TestCase):
    """A token that is a FIELD of something else, not our variable.

    The `.` in `_member_access`'s lookbehind is the only thing keeping
    these, and nothing pinned it -- a final mutation sweep found it as
    the one surviving mutant. The failure mode is the expensive one:
    the key is silently deleted, so working configuration disappears
    with no error anywhere.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_field_on_another_object_is_not_a_reference(self):
        self.assertTrue(self._survives('{{ keycloak.oidc.issuer }}'))

    def test_the_config_namespace_is_real_and_must_survive(self):
        # `config` is an actual namespace in this render context, so
        # `{{ config.oidc.url }}` is a shape a catalog entry can write.
        self.assertTrue(self._survives('{{ config.oidc.url }}'))
        self.assertTrue(self._survives('{{ config.smtp.host }}'))

    def test_a_SPACED_qualifier_is_still_a_qualifier(self):
        # `a . b` is `a.b` to Jinja. The lookbehind only sees the
        # character immediately before the token, so with spaces that
        # character is a SPACE and the token read as top-level -- the key
        # was silently deleted while accessing `config.oidc`, not the
        # integration. Python's `re` has no variable-length lookbehind,
        # so the expression is normalised before matching.
        self.assertTrue(self._survives('{{ config . oidc . url }}'))
        self.assertTrue(self._survives('{{ config.  oidc.url }}'))
        self.assertTrue(self._survives('{{ keycloak . oidc . issuer }}'))
        # And the spacing must not hide a REAL reference either.
        self.assertFalse(self._survives('{{ oidc . issuer }}'))


class ShadowedNameIsNotPoppedTests(TestCase):
    """A locally bound name is NOT popped -- and `main` pops it.

    `{% for smtp in xs %}{{ smtp.a }}{% endfor %}` binds the name
    locally, so the value never touches the integration and the key must
    survive. Every hand-written rule this module went through got that
    wrong, and so does `main`; asking Jinja which variables a template
    leaves undeclared gets the scoping right for free.

    The class was called `...IsOverPoppedTests` and its docstring
    described the false positive as accepted, which stopped being true
    when the scan moved to the parser. The test inside it was always
    asserting the correct behaviour -- only the framing was stale, which
    is the more dangerous half: someone grepping for what this module
    accepts would have read the opposite of the truth.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_main_pops_it_and_we_do_not(self):
        # Pins the comparison the docstring makes, so the claim cannot
        # rot again: this is a difference FROM `main`, not parity.
        value = '{% for smtp in [{"a": 1}] %}{{ smtp.a }}{% endfor %}'
        self.assertTrue(self._survives(value))
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        self.assertEqual(
            Template(value).render(**_compose_render_context(info)), '1')

    def test_a_shadowed_name_is_NOT_popped(self):
        # These bind `oidc` LOCALLY -- a loop variable, a `{% set %}`, a
        # macro parameter -- so the value never touches our integration
        # and the key must survive. `main` and every hand-written rule
        # here over-popped all three; asking Jinja which variables are
        # UNDECLARED gets the scoping right for free.
        for value in (
            '{% for oidc in [{"issuer": "local"}] %}{{ oidc.issuer }}{% endfor %}',
            '{% set oidc = {"issuer": "x"} %}{{ oidc.issuer }}',
            '{% macro m(oidc) %}{{ oidc.issuer }}{% endmacro %}{{ m({"issuer":"y"}) }}',
        ):
            with self.subTest(value=value):
                self.assertTrue(self._survives(value))

    def test_a_binding_in_one_scope_does_not_excuse_a_global_read_in_another(self):
        # The regression the shadow rule introduced: this binds `smtp` in
        # the macro and reads the REAL one outside it. Keeping the key
        # left `|int` to raise and abort the start.
        value = '{% macro m(smtp) %}{{ smtp.host }}{% endmacro %}{{ smtp.port|int }}'
        self.assertFalse(self._survives(value))


class CallParenthesisTests(TestCase):
    """A call's closing paren is not the integration's."""

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_call_that_launders_the_type_still_pops(self):
        # A call was briefly treated as a barrier, on the theory that
        # its result is not its operand. In practice a call is the
        # easiest way to launder the type back into something
        # unprotected: `dict(smtp)` strips the forgiving wrapper and
        # restores the literal `None` that `_UnsetIntegration.get`
        # exists to prevent, and `namespace(v=smtp).v` hands it straight
        # back. Both READ the integration, so both are dereferences.
        self.assertFalse(self._survives('{{ dict(oidc).get("issuer", "x") }}'))
        self.assertFalse(self._survives("{{ dict(smtp).get('user') }}"))
        self.assertFalse(self._survives('{{ namespace(v=smtp).v.host }}'))

    def test_a_dereference_inside_a_call_argument_still_pops(self):
        # `{{ range(smtp.port)|list }}` dereferences the type right
        # there, as the first argument. Recognising the call's paren must
        # only stop us seeing THROUGH it -- treating the name as
        # uninteresting let this reach the render and raise, aborting the
        # start, where the previous scanner popped the key.
        self.assertFalse(self._survives('{{ range(smtp.port)|list }}'))
        self.assertFalse(self._survives('{{ int(oidc.issuer) }}'))

    def test_a_bare_name_as_a_call_argument_is_not_a_dereference(self):
        # The other side: passed, not dereferenced. The binding renders
        # it, so there is nothing to strip.
        # Passing the type INTO a call is a read: the callee may do
        # anything with it, and popping is the safe direction.
        self.assertFalse(self._survives('{{ f(oidc) }}'))

    def test_a_spaced_call_paren_is_still_a_call(self):
        # `dict (oidc)` -- Jinja allows whitespace before a call's
        # paren, and it reads the integration either way.
        self.assertFalse(self._survives('{{ dict (oidc).get("issuer", "x") }}'))

    def test_a_parenthesised_reference_still_pops(self):
        # The form the parens were there for in the first place.
        self.assertFalse(self._survives('{{ (oidc).issuer }}'))
        self.assertFalse(self._survives('{{ ( oidc ) .issuer }}'))


class NestedBracesTests(TestCase):
    """A mapping's braces are not the construct's terminator."""

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_nested_mapping_does_not_end_the_expression_early(self):
        # `{{ {"a": {}} and smtp.port|int }}` ended at the mapping's
        # `}}`, so the real reference after it was never scanned, the key
        # survived, and `|int` raised at render -- where `main` popped it
        # and deployed.
        self.assertFalse(self._survives('{{ {"a": {}} and smtp.port|int }}'))

    def test_the_dict_index_form_the_catalog_uses_still_pops(self):
        self.assertFalse(
            self._survives('{{ {"tls": "ssl", "none": ""}[smtp.tls_mode] }}'),
        )



class ReferenceFormTests(TestCase):
    """Every way of reaching an unset type that RAISES at render.

    A single-level `{{ oidc.issuer }}` renders to '' under Jinja's
    default Undefined, so only CHAINED access raises -- and chained
    access is exactly the shape the catalog already ships
    (`smtp.from_address.split('@')[0]`). Each of these was missed while
    the pattern required a `.` or `[` immediately after the token.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_spaced_dot(self):
        self.assertFalse(self._survives('{{ oidc . issuer . host }}'))

    def test_parenthesised(self):
        self.assertFalse(self._survives('{{ (oidc).issuer.host }}'))

    def test_the_attr_filter_is_a_dereference_too(self):
        # `{{ smtp|attr("host") }}` reads exactly what `{{ smtp.host }}`
        # reads, but it is a Filter node, not a Getattr, so the AST walk
        # did not see it. It survived and rendered `smtp://@` -- a
        # malformed URL shipped silently, which is the class this whole
        # pass exists to stop.
        self.assertFalse(self._survives('{{ oidc|attr("issuer") }}'))
        self.assertFalse(self._survives(
            '{{ oidc|attr("issuer")|attr("host") }}'))
        self.assertFalse(self._survives(
            'smtp://{{ smtp|attr("username") }}@{{ smtp|attr("host") }}'))

    def test_attribute_taking_filters_are_dereferences(self):
        # Whether the attribute is named positionally or by keyword.
        self.assertFalse(self._survives('{{ [oidc]|selectattr("issuer")|list }}'))
        self.assertFalse(self._survives('{{ [oidc]|map(attribute="issuer")|list }}'))
        self.assertFalse(self._survives('{{ [oidc]|rejectattr("issuer")|list }}'))

    def test_an_operator_between_the_name_and_the_dot_still_pops(self):
        # A `Getattr` target is not always a bare `Name`. Requiring one
        # missed every defensive idiom a careful catalog author would
        # reach for, so THEY were the ones who got `smtp://:@` -- with
        # no log line, because the strip believed there was nothing to
        # pop and the document rendered fine.
        for value in ('{{ oidc.issuer }}',
                      '{{ (oidc).issuer }}',
                      '{{ (oidc or {}).issuer }}',
                      '{{ (oidc and oidc).issuer }}',
                      '{{ (oidc if oidc else oidc).issuer }}',
                      '{{ (oidc|default({})).issuer }}',
                      '{{ [oidc][0].issuer }}',
                      '{{ (oidc,)[0].issuer }}'):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_the_glitchtip_shape_written_defensively_still_pops(self):
        self.assertFalse(self._survives(
            'smtp://{{ (smtp or {}).user }}:{{ (smtp or {}).password }}'
            '@{{ (smtp or {}).host }}:{{ (smtp or {}).port }}'))

    def test_a_call_does_not_launder_the_type(self):
        # The seventh route, and its mirror. `namespace()` hands its
        # keyword back unchanged; `dict()` strips the forgiving wrapper.
        # One rendered `smtp://:@`, the other the literal `None`.
        self.assertFalse(self._survives('{{ namespace(v=oidc).v.issuer }}'))
        self.assertFalse(self._survives('{{ dict(oidc).get("x") }}'))

    def test_an_unrelated_name_is_not_swept_up(self):
        self.assertTrue(self._survives('{{ other.host }}'))
        self.assertTrue(self._survives('{{ (other or {}).host }}'))
        self.assertTrue(self._survives('{{ instance_url }}'))

    def test_any_filter_reading_the_type_is_a_dereference(self):
        # Enumerating the filters that DEREFERENCE was wrong twice: the
        # attribute-naming ones were missed first, then the ones that
        # read the whole mapping without naming an attribute. A filter
        # reads its operand, so the rule is now the other way round.
        for value in ("{{ oidc|urlencode }}",
                      "{% for k, v in oidc|items %}{{ k }}{% endfor %}",
                      "{{ oidc|dictsort }}",
                      "{{ oidc|list }}",
                      "{{ oidc|tojson }}",
                      "{{ oidc|attr('issuer') }}",
                      "{{ [oidc]|map('attr','issuer')|join }}",
                      "{{ [oidc]|map('upper')|list }}"):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_default_is_NOT_an_exception(self):
        # This code claimed for two rounds that `default` handles the
        # unset case and so must be preserved. It does not: the binding
        # is a DEFINED empty mapping, so `{{ oidc|default("D") }}`
        # renders `{}`, not `D`. There is no author handling to keep,
        # only garbage to remove.
        self.assertFalse(self._survives('{{ oidc|default("D") }}'))
        self.assertFalse(self._survives('{{ oidc|d("D") }}'))

    def test_a_filter_on_an_unrelated_name_is_untouched(self):
        self.assertTrue(self._survives("{{ ['a']|map('upper')|list }}"))
        self.assertTrue(self._survives('{{ instance_url|upper }}'))
        self.assertTrue(self._survives('{{ other|urlencode }}'))

    def test_aliasing_is_not_chased(self):
        # `{% set x = oidc %}{{ x.issuer.host }}` moves the dereference
        # onto another name, and no text rule can follow it. Rather than
        # grow one, the value is KEPT and the binding renders it -- which
        # is the whole reason the rule is allowed to be incomplete. On
        # main this same value raises UndefinedError and fails the start.
        # Now POPPED: `find_undeclared_variables` reports `oidc` for
        # this whole value, so the base rule catches it even though no
        # rule follows the alias. What remains uncovered is a macro
        # PARAMETER carrying it across two env values, which no
        # single-value analysis can see.
        self.assertFalse(self._survives('{% set x = oidc %}{{ x.issuer.host }}'))

    def test_plus_whitespace_control_on_endraw(self):
        # `{% raw %}` shields its body, and Jinja accepts `+` as well as
        # `-` for whitespace control on either tag. A hand-written
        # scanner got this wrong -- it ran the skip to the end of the
        # value, so everything AFTER the raw block went unexamined. The
        # parser handles it natively now, but the property still has to
        # hold, so the case is kept: what follows `{% endraw %}` is
        # scanned.
        # What follows `{% endraw %}` is examined -- and here what
        # follows is a GUARD, so it is correctly kept (it renders `x`
        # unset and `xy` configured). The property being pinned is that
        # the text after the raw block is not skipped; a read there is
        # still popped, which the next assertion covers.
        self.assertTrue(
            self._survives('{% raw %}x{%+ endraw %}{% if oidc.issuer.startswith("h") %}y{% endif %}'),
        )
        self.assertFalse(
            self._survives('{% raw %}x{%+ endraw %}{{ oidc.issuer }}'),
        )
        self.assertFalse(
            self._survives('{% raw %}x{% endraw +%}{{ oidc.issuer }}'),
        )

    def test_plus_whitespace_control_on_raw_opener(self):
        # The mirror: what a raw block SHIELDS is literal text, not a
        # reference, so it must not be popped.
        self.assertTrue(self._survives('{%+ raw %}{{ oidc.issuer }}{% endraw %}'))

    def test_a_different_variable_sharing_the_substring_is_not_a_reference(self):
        self.assertTrue(self._survives('{{ myoidc_var }}'))
        self.assertTrue(self._survives('{{ KEYCLOAK_OIDC }}'))


class UnsetBindingCannotFailTests(TestCase):
    """The safety net under the strip pass.

    The pass reads template text, and eight rounds established it cannot
    be complete: a macro argument moves the dereference onto another
    name, and `map(attribute='a.b')` hides the path inside a string
    literal the scanner has to blank. So the deploy must not DEPEND on it
    being complete.

    Two properties matter, and the second is why this is a `dict` and not
    a `ChainableUndefined`:
      1. nothing raises, whether or not the strip pass saw it;
      2. everything that worked when unset types were a plain `{}` STILL
         works. A ChainableUndefined chained correctly and stopped being
         a dict, which broke `|tojson` outright (an uncaught 500), turned
         `|int` into an UndefinedError, and wrote the literal text
         `Undefined` into the compose file -- all regressions against the
         `{}` it replaced.
    """

    def _render(self, value):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        return Template(value).render(**_compose_render_context(info))

    def _renders_without_raising(self, value):
        try:
            self._render(value)
        except Exception as exc:  # pragma: no cover - the failure we prevent
            self.fail(f'{value!r} must render, not raise: {exc!r}')

    def test_dereference_shapes_do_not_raise(self):
        # Each of these raises UndefinedError when the type is a plain
        # `{}`, and each is a shape the strip pass may or may not see.
        for value in (
            '{{ oidc.issuer.host }}',
            '{{ oidc.a.split("/")[0] }}',
            '{{ smtp.from_address.split("@")[0] }}',
            '{% with x = oidc %}{{ x.issuer.host }}{% endwith %}',
            '{% macro m(x) %}{{ x.issuer.host }}{% endmacro %}{{ m(oidc) }}',
            '{{ [oidc]|map(attribute="issuer.host")|list }}',
        ):
            with self.subTest(value=value):
                self._renders_without_raising(value)

    def test_iterating_a_FIELD_does_not_raise(self):
        # `oidc.items()` is `dict.items()` on the wrapper and never
        # reaches `_UnsetField` at all -- which is what made a dead
        # `__iter__` override look covered. These iterate a FIELD, which
        # is the case an OIDC blob actually produces (`redirect_uris`,
        # `scopes`).
        for value in (
            '{% for u in oidc.redirect_uris %}{{ u }}{% endfor %}',
            '{{ oidc.scopes|join(",") }}',
            '{{ oidc.redirect_uris|list }}',
        ):
            with self.subTest(value=value):
                self._renders_without_raising(value)

    def test_iterating_the_type_itself_does_not_raise(self):
        self._renders_without_raising(
            '{% for k, v in oidc.items() %}{{ k }}{% endfor %}',
        )

    def test_the_message_reads_as_a_dict_not_an_internal_class(self):
        # Jinja renders the object's type into the message. Passing the
        # wrapper leaked `apps.utils.docker.compose._UnsetIntegration
        # object` into a 500 an operator has to read; a plain dict gives
        # main's wording, which is what they would recognise.
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        with self.assertRaises(UndefinedError) as caught:
            Template('{{ oidc.issuer|int }}').render(**_compose_render_context(info))
        self.assertIn('dict object', str(caught.exception))
        self.assertNotIn('_UnsetIntegration', str(caught.exception))

    def test_a_failure_that_still_raises_names_the_field(self):
        # A few operations are not absorbed -- `|int`, comparisons,
        # arithmetic -- and they raise out of `create_compose` as a 500.
        # A shared no-name field made that message `None is undefined`,
        # naming neither the type nor the field, where the plain `{}`
        # binding named the field. The residual is accepted; an
        # unreadable message for it is not.
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        ctx = _compose_render_context(info)
        with self.assertRaises(UndefinedError) as caught:
            Template('{{ oidc.issuer|int }}').render(**ctx)
        self.assertIn('issuer', str(caught.exception))

    def test_a_dereference_matches_the_plain_dict_binding(self):
        # THE regression this leaf exists for. Returning `self` from
        # `__missing__` made a one-level access render the literal `{}`,
        # where a plain `{}` binding rendered `''` -- swapping a benign
        # empty value for a garbage one the container then tries to use
        # as a hostname, at the depth that is by far the most common.
        self.assertEqual(self._render('{{ oidc.issuer }}'), '')
        self.assertEqual(self._render('{{ smtp.host }}'), '')

    def test_a_default_on_a_dereference_still_fires(self):
        # A field must be UNDEFINED, not an empty mapping, or `|default`
        # silently stops working.
        self.assertEqual(self._render('{{ smtp.host|default(25) }}'), '25')

    def test_the_type_itself_is_still_an_empty_mapping(self):
        # Only the FIELDS are undefined; the type keeps `{}`'s rendering.
        self.assertEqual(self._render('{{ oidc }}'), '{}')

    def test_it_is_still_an_empty_dict(self):
        # The regression guard. Each of these worked with `{}` and MUST
        # keep working; the ChainableUndefined version broke all four.
        self.assertEqual(self._render('{{ oidc|tojson }}'), '{}')
        self.assertEqual(self._render('{{ oidc|int }}'), '0')
        self.assertEqual(self._render('{{ oidc|pprint }}'), '{}')
        self.assertEqual(self._render('{{ oidc|length }}'), '0')

    def test_real_dict_methods_keep_their_behaviour(self):
        # `.get` must be dict.get, not the chaining fallback, or the
        # author's default is silently discarded.
        self.assertEqual(self._render('{{ oidc.get("issuer", "d") }}'), 'd')

    def test_it_is_falsy_so_guards_work(self):
        self.assertEqual(self._render('{% if oidc %}on{% else %}off{% endif %}'), 'off')

    def test_a_set_integration_is_untouched(self):
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': {'oidc': {'issuer': _ISSUER}}},
        )
        self.assertEqual(_compose_render_context(info)['oidc'], {'issuer': _ISSUER})

    def test_baked_files_render_the_shapes_that_tolerate_an_empty_mapping(self):
        # The scope of the loud-refusal guarantee, asserted because the
        # comment claiming it was once wider than the truth. A
        # DEREFERENCE refuses; a reference that tolerates an empty
        # mapping renders, exactly as it does for smtp.
        info = build_render_context({
            'id': 'i1', 'integrations': {}, 'configurations': [], 'ports': [],
        })
        self.assertEqual(_render_baked_file('{{ oidc }}', info, 'r.json'), '{}')
        self.assertEqual(
            _render_baked_file('{{ oidc.issuer|default("D") }}', info, 'r.json'), 'D',
        )
        self.assertEqual(
            _render_baked_file('{% if oidc %}y{% else %}n{% endif %}', info, 'r.json'), 'n',
        )

    def test_baked_files_still_refuse_loudly(self):
        # Scoped to the compose render on purpose. A baked file is
        # CONTENT, where a silently empty value is worse than a refusal,
        # and it has no strip pass to remove the key properly.
        #
        # Built through `build_render_context`, the SHARED builder the
        # baked-file path actually uses. Constructing the context by hand
        # made this pass even with the binding leaked into that builder,
        # which is precisely the mistake it is meant to catch.
        info = build_render_context({
            'id': 'i1', 'integrations': {}, 'configurations': [], 'ports': [],
        })
        with self.assertRaises(ConfigRenderError):
            _render_baked_file('{{ oidc.issuer }}', info, 'realm.json')


class JinjaFormsTheContractCoversTests(TestCase):
    """Forms that decided a defect at some point in this feature's life.

    Each is asserted as an OUTCOME of the public function -- popped, kept
    or simply not crashing -- rather than against any particular scanning
    strategy, so they stayed meaningful when the implementation moved
    from hand-written text scanning to parsing the Jinja AST.
    """

    def _survives(self, value, **names):
        # `names` are the OTHER context names the case assumes exist.
        # The probe leaves an unbound name undefined, exactly as the
        # real render does, so a guard on `config` or `feature` means
        # something different depending on whether it is bound at all.
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context(
            dict({'id': 'i1', 'integrations': {}}, **names))
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_aliasing_needs_the_set_or_with_keyword(self):
        # A comparison is not a binding.
        self.assertTrue(self._survives('{% if x == oidc %}y{% endif %}'))

    def test_a_with_block_without_an_assignment_does_not_crash(self):
        # `{% with oidc %}` has no `=`; splitting on one regardless
        # raised IndexError.
        try:
            self._survives('{% with oidc %}y{% endwith %}')
        except Exception as exc:  # pragma: no cover - the failure we prevent
            self.fail(f'must not raise: {exc!r}')

    def test_a_dereference_anywhere_in_the_body_pops(self):
        # There is no guard concept any more: a dereference is a
        # dereference wherever it sits. Both of these pop, exactly as
        # they do on main.
        # `{{ oidc if oidc.issuer else 1 }}` is NOT here: its test
        # asserts the integration is configured, so the branch reading
        # it is unreachable when unset. See
        # `AGuardedBranchIsUnreachableWhenUnsetTests`.
        self.assertFalse(self._survives('{{ oidc and oidc.issuer }}'))

    def test_a_quote_inside_a_comment_does_not_swallow_the_terminator(self):
        # A comment has no expression syntax, so an apostrophe in it is
        # text. Tracking quotes there let it eat the `#}`.
        self.assertTrue(self._survives("{# don't {{ oidc.issuer }} #}x"))


class BindingIsWiredIntoTheRenderTests(TestCase):
    """That the binding is actually REACHED by `create_compose`.

    Everything else about `_UnsetIntegration` was testable by
    instantiating it directly, which meant the whole safety net could be
    unwired -- rendering with the raw `greffon_info` instead of
    `_compose_render_context` -- and every test still passed. Mutation
    testing found that; nothing else would have.

    The value below is the one case that distinguishes wired from
    unwired: the strip pass KEEPS it, so the binding is the only thing
    standing between it and `UndefinedError` out of the render.
    """

    # A GUARD: the strip keeps it (the mapping only decides a branch),
    # and without the binding `oidc.a.b` raises `UndefinedError` on the
    # plain `{}` -- so this is the shape that distinguishes wired from
    # unwired. The macro form used before is popped now that the base
    # rule reports every use of the name.
    MACRO = '{% if oidc.a.b %}x{% endif %}'

    def test_the_strip_pass_keeps_it(self):
        # If this ever starts being popped the test below stops testing
        # the binding, so it is asserted rather than assumed.
        compose = {'services': {'a': {'environment': {'K': self.MACRO}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        self.assertIn('K', compose['services']['a']['environment'])

    def test_create_compose_renders_it_instead_of_raising(self):
        compose = {'services': {'a': {'environment': {'K': self.MACRO}}}}
        info = {
            'id': 'inst-1',
            'configurations': [],
            'integrations': {},
            'ports': [{'port_host': 4242}],
        }
        with patch('apps.utils.docker.compose.os.makedirs'), \
             patch('apps.utils.docker.compose.os.path.exists', return_value=True), \
             patch('builtins.open', mock_open()) as m:
            create_compose(compose, info)
        rendered = m().write.call_args[0][0]
        self.assertIn('K:', rendered)


class CallerPopulatedKeysAreNotClobberedTests(TestCase):
    def test_a_preset_value_wins_over_the_binding(self):
        # Same non-clobbering rule `_compute_integrations_context`
        # follows: a caller that deliberately populated the key keeps it.
        info = {'id': 'i1', 'integrations': {}, 'oidc': {'preset': 1}}
        self.assertEqual(_compose_render_context(info)['oidc'], {'preset': 1})


class PopLoggingTests(TestCase):
    """The pop is inferred from template text, so when it is wrong the
    greffon boots without the variable and fails at runtime. The log line
    is the only thing connecting the two."""

    def _pop_logs(self, env):
        compose = {'services': {'grafana': {'environment': env}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        with self.assertLogs('apps.utils.docker.compose', level=logging.INFO) as cm:
            _delete_unset_integration_env_keys(compose, info)
        return cm.output

    def test_names_the_key_and_service(self):
        [line] = self._pop_logs({'OIDC_ISSUER_URL': '{{ oidc.issuer }}'})
        self.assertIn('OIDC_ISSUER_URL', line)
        self.assertIn('grafana', line)

    def test_names_only_the_type_that_matched(self):
        # Both types are unset here. Reporting the whole unset set would
        # say "smtp, oidc" for a key that references only oidc.
        [line] = self._pop_logs({'OIDC_ISSUER_URL': '{{ oidc.issuer }}'})
        self.assertIn('oidc', line)
        self.assertNotIn('smtp', line)

    def test_does_not_log_the_value(self):
        # Values can hold rendered credentials.
        [line] = self._pop_logs({'K': '{{ oidc.issuer }}SECRET-abc123'})
        self.assertNotIn('SECRET-abc123', line)

    def test_list_form_logs_the_key_not_the_pair(self):
        [line] = self._pop_logs(['OIDC_ISSUER_URL={{ oidc.issuer }}'])
        self.assertIn('OIDC_ISSUER_URL', line)
        self.assertNotIn('{{ oidc.issuer }}', line)


class OIDCRenderEndToEndTests(TestCase):
    """The ordering trap, as an executable check."""

    def _render(self, integrations):
        compose = {
            'services': {
                'grafana': {'environment': {'OIDC_ISSUER_URL': '{{ oidc.issuer }}'}},
            },
        }
        info = {
            'id': 'inst-1',
            'configurations': [
                {'destinations': [
                    {'type': 'oidc', 'key': 'OIDC_ISSUER_URL', 'container': 'grafana'},
                ]},
            ],
            'integrations': integrations,
            'ports': [{'port_host': 4242}],
        }
        with patch('apps.utils.docker.compose.os.makedirs'), \
             patch('apps.utils.docker.compose.os.path.exists', return_value=True), \
             patch('builtins.open', mock_open()) as m:
            create_compose(compose, info)
        return m().write.call_args[0][0]

    def test_set_oidc_substitutes_issuer(self):
        rendered = self._render({'oidc': {'issuer': _ISSUER}})
        self.assertIn(_ISSUER, rendered)
        self.assertIn('OIDC_ISSUER_URL:', rendered)
        self.assertNotIn('{{ oidc.issuer }}', rendered)

    def test_unset_oidc_renders_instead_of_raising(self):
        # THE regression test. With `oidc` absent from
        # KNOWN_INTEGRATION_TYPES this raises
        # `UndefinedError: 'oidc' is undefined` and the instance never
        # deploys. Asserted as "does not raise, and the key is gone".
        try:
            rendered = self._render({})
        except UndefinedError as exc:  # pragma: no cover - the failure we prevent
            self.fail(f'unset oidc must render, not raise: {exc}')
        self.assertNotIn('OIDC_ISSUER_URL', rendered)
