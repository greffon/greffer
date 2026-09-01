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

from jinja2 import Template, UndefinedError

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


class ConstructScanningTests(TestCase):
    """What counts as a reference to an unset type.

    The rule the pop pass has to satisfy: after it runs, no surviving
    value may raise when rendered with the type bound to `{}`, and no
    value that would have rendered fine may be dropped. Each case below
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
        self.assertFalse(self._survives('{{ "a\\"b" ~ oidc.issuer }}'))

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

    def test_attr_filter(self):
        self.assertFalse(self._survives('{{ oidc|attr("issuer")|attr("host") }}'))

    def test_aliased_through_set(self):
        # No adjacency rule can catch this one, which is why the rule is
        # a bare token match rather than a tighter pattern.
        self.assertFalse(self._survives('{% set x = oidc %}{{ x.issuer.host }}'))

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


class GuardedFallbackTests(TestCase):
    """A value whose author HANDLED the unset case must survive.

    These are the idioms an entry uses to degrade gracefully. Their
    intended output is the fallback, not an absent env var, and a rule
    that popped them regressed `smtp` -- which has been shipping since
    Feature #4 -- on every instance with no SMTP configured.
    """

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_truthiness_guard_survives(self):
        self.assertTrue(self._survives('{% if oidc %}on{% else %}off{% endif %}'))
        self.assertTrue(self._survives('{% if smtp %}on{% else %}off{% endif %}'))

    def test_default_filter_survives(self):
        self.assertTrue(self._survives("{{ oidc|default('none') }}"))
        self.assertTrue(self._survives("{{ smtp|default('none') }}"))

    def test_is_defined_survives(self):
        self.assertTrue(self._survives("{{ 'y' if smtp is defined else 'n' }}"))

    def test_a_shadowing_binding_is_not_a_reference(self):
        # The name is being BOUND here, not read from the context.
        self.assertTrue(self._survives("{% set oidc = 'x' %}{{ oidc }}"))
        self.assertTrue(self._survives("{% for oidc in ['a'] %}{{ oidc }}{% endfor %}"))

    def test_a_field_of_that_name_on_another_object_is_not_a_reference(self):
        # `oidc` here belongs to `keycloak`, not to the context.
        self.assertTrue(self._survives('{{ keycloak.oidc.issuer }}'))

    def test_chained_access_still_pops(self):
        # The other side: these RAISE, so they must go.
        self.assertFalse(self._survives('{{ oidc.issuer.host }}'))
        self.assertFalse(self._survives('{{ smtp.from_address.split("@")[0] }}'))


class UnsetBindingCannotFailTests(TestCase):
    """The safety net under the strip pass.

    The pass reads template text, and seven rounds established it cannot
    be complete: aliasing moves the dereference to another name, and
    `map(attribute='a.b')` hides the path inside a string literal the
    scanner blanks. So the deploy must not DEPEND on it being complete.
    Every shape below renders empty instead of raising, whether or not
    the strip pass saw it.
    """

    def _render(self, value):
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        return Template(value).render(**_compose_render_context(info))

    def test_attribute_chain_does_not_raise(self):
        self.assertEqual(self._render('{{ oidc.issuer.host }}'), '')

    def test_method_calls_do_not_raise(self):
        # ChainableUndefined alone still raises on a CALL, and
        # `{{ smtp.from_address.split('@')[0] }}` is a shape the catalog
        # already ships -- so calls have to be absorbed too.
        self.assertEqual(self._render('{{ oidc.get("issuer", "d") }}'), '')
        self.assertEqual(self._render('{{ smtp.from_address.split("@")[0] }}'), '')

    def test_iteration_does_not_raise(self):
        self.assertEqual(
            self._render('{% for k, v in oidc.items() %}{{ k }}{% endfor %}'), '',
        )

    def test_aliased_and_macro_forms_do_not_raise(self):
        # The two the strip pass structurally cannot follow.
        self.assertEqual(
            self._render('{% with x = oidc %}{{ x.issuer.split("@")[0] }}{% endwith %}'), '',
        )
        self.assertEqual(
            self._render('{% macro m(x) %}{{ x.issuer.host }}{% endmacro %}{{ m(oidc) }}'), '',
        )

    def test_the_binding_is_still_falsy_so_guards_work(self):
        # It must not become truthy, or every `{% if smtp %}` would take
        # the wrong branch.
        self.assertEqual(self._render('{% if oidc %}on{% else %}off{% endif %}'), 'off')
        self.assertEqual(self._render('{{ oidc|default("none") }}'), 'none')

    def test_a_set_integration_is_untouched(self):
        info = _compute_integrations_context(
            {'id': 'i1', 'integrations': {'oidc': {'issuer': _ISSUER}}},
        )
        ctx = _compose_render_context(info)
        self.assertEqual(ctx['oidc'], {'issuer': _ISSUER})

    def test_baked_files_still_refuse_loudly(self):
        # Scoped to the compose render on purpose. A baked file is
        # CONTENT, where a silently empty value is worse than a refusal,
        # and it has no strip pass to remove the key properly.
        #
        # Built through `build_render_context`, the SHARED builder the
        # baked-file path actually uses, not by calling
        # `_compute_integrations_context` directly. Constructing the
        # context by hand made this pass even with the permissive binding
        # leaked into the shared builder, which is precisely the mistake
        # it is meant to catch.
        info = build_render_context({
            'id': 'i1', 'integrations': {}, 'configurations': [], 'ports': [],
        })
        with self.assertRaises(ConfigRenderError):
            _render_baked_file('{{ oidc.issuer }}', info, 'realm.json')


class WithAliasingTests(TestCase):
    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_with_binds_an_alias_like_set(self):
        self.assertFalse(
            self._survives('{% with x = oidc %}{{ x.issuer.host }}{% endwith %}'),
        )


class GuardSpansTheWholeValueTests(TestCase):
    """The guard and the access it protects live in different bodies."""

    def _survives(self, value):
        compose = {'services': {'a': {'environment': {'K': value}}}}
        info = _compute_integrations_context({'id': 'i1', 'integrations': {}})
        _delete_unset_integration_env_keys(compose, info)
        return 'K' in compose['services']['a']['environment']

    def test_guard_in_one_body_protects_access_in_another(self):
        # `{% if smtp %}` and `{{ smtp.host }}` are two constructs.
        # Judging each alone sees only the bare access and pops the key
        # the guard exists to preserve.
        self.assertTrue(
            self._survives('{% if smtp %}{{ smtp.host }}{% else %}localhost{% endif %}'),
        )

    def test_default_filter_on_a_dereference_protects_it(self):
        self.assertTrue(self._survives('{{ oidc.issuer|default("http://d") }}'))

    def test_ternary_guard_protects_it(self):
        self.assertTrue(self._survives('{{ oidc.issuer if oidc else "none" }}'))

    def test_an_unguarded_dereference_still_pops(self):
        self.assertFalse(self._survives('{{ oidc.issuer.host }}'))


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
