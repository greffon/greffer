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
from unittest import TestCase
from unittest.mock import mock_open, patch

import yaml

from jinja2 import Template, UndefinedError
from jinja2.exceptions import TemplateSyntaxError

from apps.utils.docker.compose import (
    KNOWN_INTEGRATION_TYPES,
    ConfigRenderError,
    _compose_render_context,
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
    is the NORMAL state for `oidc` -- and it used to be the worst of the
    three: "set" enough that neither pass strips its keys, but a plain
    dict, so the binding never applied either. Both safety nets bypassed
    at once.
    """

    PARTIAL = {'oidc': {'client_id': 'x'}}

    def _render(self, template, integrations):
        info = _compute_integrations_context({'id': 'i1', 'integrations': integrations})
        return Template(template).render(**_compose_render_context(info))

    def test_a_present_field_is_untouched(self):
        self.assertEqual(self._render('{{ oidc.client_id }}', self.PARTIAL), 'x')

    def test_an_absent_field_renders_empty(self):
        self.assertEqual(self._render('{{ oidc.issuer }}', self.PARTIAL), '')

    def test_chaining_off_an_absent_field_does_not_raise(self):
        # This was an UndefinedError out of `create_compose`, i.e. a 500
        # with no service started.
        try:
            self.assertEqual(
                self._render('{{ oidc.issuer.split("/")[0] }}', self.PARTIAL), '',
            )
        except Exception as exc:  # pragma: no cover - the failure we prevent
            self.fail(f'a partial blob must not raise: {exc!r}')

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


class LocallyBoundNameTests(TestCase):
    """A name Jinja BINDS is not our variable.

    `{% for oidc in ... %}`, `{% set oidc = ... %}` and a macro parameter
    named `oidc` all shadow the integration, so the member access after
    them resolves against the local value and renders. Popping the key
    deleted working configuration silently -- the expensive failure
    direction.
    """

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_a_loop_variable_shadows_the_type(self):
        value = '{% for oidc in [{"issuer": "local"}] %}{{ oidc.issuer }}{% endfor %}'
        self.assertEqual(Template(value).render(), 'local')
        self.assertTrue(self._survives(value))

    def test_a_set_binding_shadows_the_type(self):
        value = '{% set oidc = {"issuer": "x"} %}{{ oidc.issuer }}'
        self.assertEqual(Template(value).render(), 'x')
        self.assertTrue(self._survives(value))

    def test_a_macro_parameter_shadows_the_type(self):
        value = '{% macro m(oidc) %}{{ oidc.issuer }}{% endmacro %}{{ m({"issuer":"y"}) }}'
        self.assertEqual(Template(value).render(), 'y')
        self.assertTrue(self._survives(value))

    def test_an_unshadowed_reference_still_pops(self):
        self.assertFalse(self._survives('{{ oidc.issuer }}'))


class OpenerScanCostTests(TestCase):
    """The construct scan must be linear in the value's length.

    Searching for each opener kind separately re-scanned the whole
    remaining suffix per construct, so a value with many `{{ }}` blocks
    was quadratic -- and this runs on the instance-start path, where the
    value comes from the catalog.
    """

    def _time(self, blocks):
        import time
        value = '{{ x }}' * blocks
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        start = time.perf_counter()
        _delete_unset_integration_env_keys(compose, info)
        return time.perf_counter() - start

    def test_doubling_the_input_does_not_quadruple_the_work(self):
        # Timing-based, so the bound is deliberately loose: quadratic
        # showed a ~4x ratio, linear shows ~2x. A 3x ceiling separates
        # them without going flaky on a loaded machine.
        self._time(2000)  # warm up the regex cache
        small = self._time(4000)
        large = self._time(8000)
        self.assertLess(large, small * 3, f'{small:.4f}s -> {large:.4f}s looks quadratic')


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


class ScannerRulesTheCommentsAssertTests(TestCase):
    """Rules the implementation states in prose and nothing checked.

    Each of these survived mutation: the code could be changed to
    contradict its own comment and the suite stayed green.
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
