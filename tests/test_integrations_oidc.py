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

import logging
import os
import tempfile
from unittest import TestCase
from unittest.mock import mock_open, patch

import yaml

from jinja2 import Environment, Template, UndefinedError
from jinja2.exceptions import TemplateAssertionError, TemplateSyntaxError

from apps.utils.docker import compose as compose_module
from apps.utils.docker.compose import (
    KNOWN_INTEGRATION_TYPES,
    ConfigRenderError,
    _UnsetIntegration,
    _compose_render_context,
    _MAX_UNDO_RENDERS,
    _UNDO_CHUNK,
    _document_renders,
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

    def test_a_non_string_env_value_is_kept(self):
        # YAML gives `K: 1` an int, and `K: [a]` a list. Stringifying
        # instead of skipping would scan their repr for Jinja.
        compose = self._run({'services': {'app': {'environment': {
            'N': 1, 'B': True, 'L': ['{{ smtp.host }}'], 'NONE': None,
        }}}})
        self.assertEqual(
            compose['services']['app']['environment'],
            {'N': 1, 'B': True, 'L': ['{{ smtp.host }}'], 'NONE': None},
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

    TWO rules pop, not one: the value names the integration, OR it
    carries a `{%` tag (see `ABlockSplitAcrossValuesIsPoppedWholeTests`
    for why the second exists). A value matching neither is kept, so an
    unrelated unparseable value is not silently dropped from a deploy --
    it fails the render loudly, exactly as it does on `main`.
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


class TheUndoIsBoundedTests(TestCase):
    """The undo is chunked and budgeted, and degrades without regressing.

    Each attempt costs a full dump+compile+render, so deciding one key
    at a time is quadratic. Keys are tried in batches and only a failing
    batch is split.

    Crucially, running out of budget undoes only the keys not yet
    DECIDED. Restoring everything wholesale was the earlier behaviour
    and it brought back the very bug this function exists to prevent:
    glitchtip's `EMAIL_URL` rendering `smtp://:@`.
    """

    def _compose(self, n, with_email=True):
        env = {}
        if with_email:
            env['A_EMAIL_URL'] = 'smtp://{{ smtp.username }}@{{ smtp.host }}'
        env.update({f'K{i:04d}': '{{ oidc.issuer }}' for i in range(n)})
        env['Y_OPEN'] = '{# note'
        env['Z_CLOSE'] = '{{ oidc.host }} #}'
        return {'services': {'app': {'environment': env}}}

    def _run(self, compose):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return compose['services']['app']['environment'], info

    def test_a_small_case_is_decided_selectively(self):
        env, info = self._run(self._compose(5))
        self.assertNotIn('K0000', env)      # clean pops survive
        self.assertNotIn('A_EMAIL_URL', env)
        self.assertIn('Y_OPEN', env)        # the straddling pair is undone
        Template(yaml.dump({'services': {'app': {'environment': env}}})).render(
            **_compose_render_context(info))

    def test_a_batch_that_renders_is_kept_whole(self):
        # More keys than one chunk, all clean: still fully popped.
        env, _ = self._run(self._compose(_UNDO_CHUNK * 3))
        self.assertNotIn('A_EMAIL_URL', env)
        self.assertFalse([k for k in env if k.startswith('K')])

    def test_beyond_the_budget_the_earlier_decisions_still_hold(self):
        # The regression guard: exhausting the budget must NOT resurrect
        # the malformed URL. The budget is patched rather than reached
        # with a huge compose -- the behaviour is what matters, and a
        # 1600-key document costs seconds to render repeatedly.
        with patch.object(compose_module, '_MAX_UNDO_RENDERS', 2):
            env, _ = self._run(self._compose(_UNDO_CHUNK * 4))
        self.assertNotIn('A_EMAIL_URL', env)

    def test_beyond_the_budget_the_document_still_renders(self):
        compose = self._compose(_UNDO_CHUNK * 4)
        with patch.object(compose_module, '_MAX_UNDO_RENDERS', 2):
            _, info = self._run(compose)
        Template(yaml.dump(compose)).render(**_compose_render_context(info))

    def test_beyond_the_budget_the_undecided_keys_are_left_alone(self):
        with patch.object(compose_module, '_MAX_UNDO_RENDERS', 2):
            env, _ = self._run(self._compose(_UNDO_CHUNK * 4))
        # Some clean pops were never decided, so they survive un-popped.
        self.assertTrue([k for k in env if k.startswith('K')])

    def test_exhausting_the_budget_is_logged(self):
        with patch.object(compose_module, '_MAX_UNDO_RENDERS', 2):
            with self.assertLogs(
                'apps.utils.docker.compose', level='WARNING',
            ) as caught:
                self._run(self._compose(_UNDO_CHUNK * 4))
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
        for bad in ('garbage', 7, [], ['smtp'], None, True):
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

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
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
            '{% if oidc.issuer.startswith("https") %}a{% endif %}',
            '{% for s in oidc.scopes %}{{ s }}{% endfor %}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_statement_blocks_are_scanned_for_smtp_too(self):
        # Not an oidc-only rule; the same hole existed for the type that
        # was already shipping.
        self.assertFalse(self._survives('{% if smtp.host %}x{% endif %}'))

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

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_reference_after_a_quoted_closing_delimiter_is_found(self):
        self.assertFalse(self._survives('{{ "}}" ~ oidc.issuer.split("@")[0] }}'))

    def test_reference_after_a_quoted_block_delimiter_is_found(self):
        self.assertFalse(
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
        self.assertFalse(self._survives('{%- if oidc.issuer -%}a{%- endif -%}'))
        self.assertTrue(self._survives('{%- raw -%}{{ oidc.issuer }}{%- endraw -%}'))


class NotOurVariableTests(TestCase):
    """A token that is a FIELD of something else, not our variable.

    The `.` in `_member_access`'s lookbehind is the only thing keeping
    these, and nothing pinned it -- a final mutation sweep found it as
    the one surviving mutant. The failure mode is the expensive one:
    the key is silently deleted, so working configuration disappears
    with no error anywhere.
    """

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
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


class ShadowedNameIsOverPoppedTests(TestCase):
    """A locally bound name is popped, exactly as `main` pops it.

    `{% for oidc in ... %}` binds the name, so the access after it
    renders and popping the key is a false positive. It is accepted --
    `main` has the same one -- because the rule that removed it made
    something worse: suppressing on ANY binding in the value kept a
    genuine reference when one scope bound the name and another read the
    real global, and `create_compose` then 500'd where `main` had simply
    popped the key.

    Scope-accurate detection needs Jinja's parser. Between two
    imprecisions, this is the one that costs an env var rather than the
    deploy.
    """

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_shadowed_name_is_popped_like_main_does(self):
        for value in (
            '{% for oidc in [{"issuer": "local"}] %}{{ oidc.issuer }}{% endfor %}',
            '{% set oidc = {"issuer": "x"} %}{{ oidc.issuer }}',
            '{% macro m(oidc) %}{{ oidc.issuer }}{% endmacro %}{{ m({"issuer":"y"}) }}',
        ):
            with self.subTest(value=value):
                self.assertFalse(self._survives(value))

    def test_a_binding_in_one_scope_does_not_excuse_a_global_read_in_another(self):
        # The regression the shadow rule introduced: this binds `smtp` in
        # the macro and reads the REAL one outside it. Keeping the key
        # left `|int` to raise and abort the start.
        value = '{% macro m(smtp) %}{{ smtp.host }}{% endmacro %}{{ smtp.port|int }}'
        self.assertFalse(self._survives(value))


class CallParenthesisTests(TestCase):
    """A call's closing paren is not the integration's."""

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_call_result_is_not_the_integration(self):
        # `\)*` in the pattern crossed the call's paren, so this matched
        # `oidc).get` and the key was deleted -- while it renders
        # `fallback` perfectly well.
        value = '{{ dict(oidc).get("issuer", "fallback") }}'
        self.assertEqual(Template(value).render(oidc={}), 'fallback')
        self.assertTrue(self._survives(value))

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
        self.assertTrue(self._survives('{{ f(oidc) }}'))

    def test_a_call_result_is_not_the_integration_with_a_spaced_paren(self):
        # `dict (oidc)` -- Jinja allows whitespace before a call's paren,
        # and a lookbehind on the character before `(` saw the space and
        # read it as grouping. The lexer sees the callee token itself.
        value = '{{ dict (oidc).get("issuer", "fallback") }}'
        self.assertEqual(Template(value).render(oidc={}), 'fallback')
        self.assertTrue(self._survives(value))

    def test_a_parenthesised_reference_still_pops(self):
        # The form the parens were there for in the first place.
        self.assertFalse(self._survives('{{ (oidc).issuer }}'))
        self.assertFalse(self._survives('{{ ( oidc ) .issuer }}'))


class NestedBracesTests(TestCase):
    """A mapping's braces are not the construct's terminator."""

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
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

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_spaced_dot(self):
        self.assertFalse(self._survives('{{ oidc . issuer . host }}'))

    def test_parenthesised(self):
        self.assertFalse(self._survives('{{ (oidc).issuer.host }}'))

    def test_the_attr_filter_is_not_chased_either(self):
        # Kept -- but NOT saved by the binding, which is worth stating
        # because it is the one shape where the binding does not hold.
        # `do_attr` uses plain getattr and returns the environment's own
        # `Undefined`, not `_UnsetField`, so a second `attr` raises. Main
        # keeps and raises on this too, so it is parity rather than a
        # regression, and popping it would need the alias-chasing the cut
        # removed on purpose.
        self.assertTrue(self._survives('{{ oidc|attr("issuer")|attr("host") }}'))
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        with self.assertRaises(UndefinedError):
            Template('{{ oidc|attr("issuer")|attr("host") }}').render(
                **_compose_render_context(info),
            )

    def test_aliasing_is_not_chased(self):
        # `{% set x = oidc %}{{ x.issuer.host }}` moves the dereference
        # onto another name, and no text rule can follow it. Rather than
        # grow one, the value is KEPT and the binding renders it -- which
        # is the whole reason the rule is allowed to be incomplete. On
        # main this same value raises UndefinedError and fails the start.
        self.assertTrue(self._survives('{% set x = oidc %}{{ x.issuer.host }}'))

    def test_plus_whitespace_control_on_endraw(self):
        # Jinja accepts `+` as well as `-`. Missing it made the raw skip
        # run to the end of the value, so everything after the raw block
        # went unscanned.
        self.assertFalse(
            self._survives('{% raw %}x{%+ endraw %}{% if oidc.issuer.startswith("h") %}y{% endif %}'),
        )
        self.assertFalse(
            self._survives('{% raw %}x{% endraw +%}{% if oidc.issuer.startswith("h") %}y{% endif %}'),
        )

    def test_plus_whitespace_control_on_raw_opener(self):
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

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
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
        self.assertFalse(self._survives('{{ oidc if oidc.issuer else 1 }}'))
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
    unwired: the strip pass KEEPS it (the bare `oidc` reads as a guard,
    and the dereference happens on a macro parameter it cannot follow),
    so the binding is the only thing standing between it and
    `UndefinedError` out of the render.
    """

    MACRO = '{% macro m(o) %}{{ o.issuer.host }}{% endmacro %}{{ m(oidc) }}'

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
