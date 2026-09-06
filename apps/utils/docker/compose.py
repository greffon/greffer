import yaml
import asyncio
import copy
import re
import json
import logging
from datauri import DataURI
from jinja2 import ChainableUndefined, Environment, StrictUndefined, Template, meta, nodes
from jinja2.exceptions import SecurityError, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment
import docker
import subprocess
import os
from urllib.parse import urlparse
# Per-instance stats reads fan multiple concurrent ``container.stats()`` calls
# at the daemon (see observe._digest_all). docker-py's default connection pool
# (DEFAULT_MAX_POOL_SIZE=10) would force those concurrent calls to contend for
# a handful of sockets and churn connections, so we raise the ceiling well
# above the stats fan-out. This is a pool CAP (lazily filled), not a
# preallocation, so it costs nothing when idle and is shared by all daemon
# calls.
client = docker.from_env(max_pool_size=32)

from apps.utils.docker.volume import docker_copy_file_into_volume, docker_create_volume, docker_is_volume_exist

logger = logging.getLogger(__name__)

# Strict Jinja environment for rendering baked `file`/`json` destination
# contents (feature: baked-config-files). Unlike the lenient ``Template``
# used for the compose body, a missing/typo'd variable here MUST raise
# rather than render to empty string: a baked Keycloak realm with an empty
# ``{{ config.OIDC_RP_CLIENT_SECRET }}`` is a silent security failure, so we
# fail the deploy loudly instead. ``autoescape=False`` because these are
# config files (JSON/conf), not HTML, and we must not HTML-escape values.
#
# SANDBOXED: the template string is catalog-author-controlled, and the catalog
# is community-extensible, so we must assume it can be hostile. A plain
# Environment would allow server-side template injection -> RCE on this worker
# (which holds the instance's minted secrets, the greffer's manager token, and
# Docker socket access), e.g.
# ``{{ cycler.__init__.__globals__.os.popen('id').read() }}``. SandboxedEnvironment
# blocks attribute traversal to unsafe objects while still rendering the
# legitimate ``{{ config.X }}`` / ``{{ instance_url }}`` references. A
# SecurityError surfaces as a clean ConfigRenderError (-> 422), like any other
# render failure.
#
# StrictUndefined makes a missing/typo'd variable raise rather than render empty
# (a baked secret silently becoming '' is a security failure). The catalog
# validator is a SEPARATE, author-facing layer that rejects StrictUndefined
# *bypass idioms* (``config.get('X')`` / ``| default``) and integration refs;
# it is NOT an SSTI gate — the sandbox is what stops injection.
# ``autoescape=False`` because these are config files (JSON/conf), not HTML.
_FILE_RENDER_ENV = SandboxedEnvironment(
    undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
)


class ConfigRenderError(Exception):
    """A render-flagged ``file``/``json`` destination failed to template.

    Raised out of ``apply_configuration`` and caught by the ``start`` router,
    which re-raises it as an HTTP 422 so the manager (and operator) get a
    clean, structured failure instead of an opaque 500.
    """

def get_nginx_service(greffon):
    #@Todo should handle conflict port
    # L4 (Tier-C) ports are NOT proxied by nginx (it cannot carry raw TCP/UDP);
    # they are published directly on their owning service in
    # create_compose_template_from_greffon. The enumerate index is over the FULL
    # ports list so {{ports[i].port_host}} still resolves correctly after the
    # L4 entries are filtered out.
    return {
        'image': 'nginx:1.20.2-alpine-perl',
        'restart': 'unless-stopped',
        'ports': [
            ('{{ports[%s].port_host}}:%s' % (i, port['port_container']))
            for i, port in enumerate(greffon['ports'])
            if port.get('exposure_tier', 'http') != 'l4'
        ],
        'networks': [greffon['internal_network']],
    }


def create_compose_template_from_greffon(compose, greffon_info):
    for service_name, service in compose['services'].items():
        service['ports'] = []
        service['volumes'] = []
        service['networks'] = []
        if 'container_name' in service:
            del service['container_name']
    compose['services']['greffon_nginx'] = get_nginx_service(greffon_info)
    # Publish L4 (Tier-C) ports directly on their owning service, bypassing the
    # nginx sidecar. proxy mode binds the public interface (0.0.0.0); tunnel
    # mode binds host-internal (127.0.0.1), reachable by the rathole-client.
    # The /<proto> suffix selects raw TCP or UDP. Keyed by the original service
    # name (container_name) before the rename below.
    l4_bind_host = greffon_info.get('l4_bind_host', '0.0.0.0')
    for i, port in enumerate(greffon_info['ports']):
        if port.get('exposure_tier', 'http') != 'l4':
            continue
        proto_suffix = '/udp' if port.get('protocol') == 'udp' else ''
        # same_port: advertise == listen == public. The container-side port
        # depends on the publish mode, because the public port differs:
        #   proxy  -> the public port IS the greffer host port. Publish
        #             host P -> container P so the app binds == publishes ==
        #             advertises one number ({{ instance_l4_port }} == port_host
        #             in proxy mode).
        #   tunnel -> the public port is the rathole relay's tunnel_port
        #             (manager-allocated, handed off as {{ instance_l4_port }});
        #             the host port_host is just the loopback port the
        #             rathole-client dials. Publish host port_host -> container
        #             tunnel_port so the app binds the SAME port it is advertised
        #             on ({{ instance_l4_port }}), and advertise == listen holds
        #             through the relay. (instance_l4_* is singular: one L4
        #             endpoint per instance, so a single tunnel_port suffices.)
        # Non-same_port: host port_host -> declared container port (unchanged).
        if port.get('same_port'):
            container_side = (
                '{{ instance_l4_port }}' if l4_bind_host == '127.0.0.1'
                else '{{ports[%s].port_host}}' % i)
        else:
            container_side = port['port_container']
        mapping = '%s:{{ports[%s].port_host}}:%s%s' % (
            l4_bind_host, i, container_side, proto_suffix)
        service = compose['services'][port['container_name']]
        service.setdefault('ports', [])
        service['ports'].append(mapping)
    for _,volume in greffon_info['volumes'].items():
        for container_name, container in volume['containers'].items():
            compose['services'][container_name].setdefault('volumes', [])
            compose['services'][container_name]['volumes'].append(f'{volume["value"]}:{container["path"]}')
    for _,network in greffon_info['networks'].items():
        for _, container_name in enumerate(network['containers']):
            compose['services'][container_name]['networks'].append(network['value'])
    
    
    compose['volumes'] = { volume['value']: {'name': volume['value'] } for _,volume in greffon_info['volumes'].items()}
    compose['networks'] = { network['value']: {} for _,network in greffon_info['networks'].items()}
    compose['services'] = { greffon_info['services'][service_name]['value']: service for service_name, service in compose['services'].items() }
    return compose


def get_compose_template(compose, greffon_info):
    compose = create_compose_template_from_greffon(compose, greffon_info)
    return compose


def get_greffon_path(greffon_info):
    path = os.path.join(os.getenv('GREFFON_PATH', '/data'), greffon_info['id'])
    isExist = os.path.exists(path)
    if not isExist: 
        os.makedirs(path)
    return path

# Feature #4 (integrations): the set of integration types the catalog
# may reference via `{{ <type>.<field> }}` in compose YAML AND via
# `destination.type: <type>` in metadata.json. New types slot in
# additively here AND in the manager (per-type FK on GreffonInstance)
# AND in the catalog validator.
#
# `oidc` is listed here BEFORE anything in the catalog references it,
# and that order is deliberate. This tuple is the gate on both passes
# below: a type absent from it is never lifted into the Jinja context,
# so a catalog entry containing `{{ oidc.issuer }}` shipped first would
# raise `UndefinedError: 'oidc' is undefined` at render and the instance
# would never deploy -- the same failure class as the known-broken
# `nextcloud/1.0`. Listing the type first makes an unset OIDC integration
# strip those env keys instead.
#
# That covers `services[*].environment` and ONLY that. Be precise about
# the limit, because it is the half Feature #3 will need: a baked config
# file (`_render_baked_file`) renders under `StrictUndefined`, where
# `oidc = {}` still raises on `{{ oidc.issuer }}` -- verified, it gives
# `'dict object' has no attribute 'issuer'` and a 422. The shapes that
# TOLERATE an empty mapping do now render, though, which is less than
# "raises either way" would suggest: `{{ oidc }}` and `|tojson` give
# `{}`, `.get(k, d)` gives `d`, and `{% if oidc %}` takes the else
# branch. Same semantics `smtp` has shipped with.
#
# `|default` depends on WHAT it is applied to, and the difference is
# easy to get backwards. The mapping is DEFINED -- an empty dict -- so
# `{{ oidc|default('d') }}` renders `{}`, not `d`. A missing FIELD is
# undefined, so `{{ oidc.issuer|default('d') }}` does render `d`.
#
# So the loud-refusal guarantee covers a DEREFERENCE, not every
# reference -- a realm file written with `|default` on a field renders
# that default rather than refusing. A Keycloak-style realm file referencing
# `{{ oidc.* }}` is still NOT made deployable by this change, and
# whoever writes the client-injection half needs a real value there
# rather than an empty default.
#
# Note this greffer half is independent of how the manager REGISTERS an
# OIDC client (platform-identity Feature #3, manager side). All the
# greffer does is thread whatever per-type blob the manager sends into
# the render; it holds no provider credential and makes no call to the
# provider.
KNOWN_INTEGRATION_TYPES = ('smtp', 'oidc')


# Distinguishes `.get(k)` from `.get(k, none)`. Using `None` as the
# sentinel treated an explicitly passed `none` as "no default", so
# `{{ oidc.get('issuer', none) is none }}` took the opposite branch from
# a plain empty dict -- the one behaviour this wrapper exists to keep.
_OMITTED = object()


class _UnsetField(ChainableUndefined):
    """A field of an integration the user did not configure.

    Undefined, exactly as it was when unset types were a plain `{}`, so
    `{{ smtp.host }}` renders `''` and `{{ smtp.host|default(25) }}`
    renders `25` -- both matching that binding byte for byte.

    What it adds is that going DEEPER cannot fail: `ChainableUndefined`
    survives an attribute chain but still raises when CALLED, and
    `{{ smtp.from_address.split('@')[0] }}` is a shape the catalog ships,
    so `__call__` is absorbed. Iteration needs no override: the base
    `Undefined` already yields nothing, and only `StrictUndefined`
    refuses -- an explicit `__iter__` here was dead code. The result is strictly
    better than the old binding rather than different from it: identical
    wherever `{}` rendered, and rendering where `{}` raised.
    """

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return self


class _UnsetIntegration(dict):
    """How an integration type the user did not configure is bound at
    COMPOSE render time.

    The env-key strip pass below removes the keys a catalog entry
    declares for an unset type, but it works by reading template TEXT,
    and eight adversarial rounds established it cannot be made complete:
    a macro argument moves the dereference onto another name, and
    `map(attribute='a.b')` hides the path inside a string literal the
    scanner has to blank for other reasons. Each round closed one hole
    and the next found another, which is the signature of an undecidable
    question rather than a bug.

    So the deploy must not DEPEND on the scan being complete. This is a
    real `dict`, so everything that worked when unset types were bound to
    a plain `{}` still works -- `|tojson` gives `{}`, `|int` gives `0`,
    `|pprint` gives `{}`, `.get('k', 'd')` gives `'d'`, `{% if oidc %}`
    is falsy -- and its FIELDS are `_UnsetField`, which keeps `{}`'s
    behaviour one level down (`{{ oidc.issuer }}` renders `''`,
    `|default` fires) while chaining deeper instead of raising.

    Checked shape by shape against a plain `{}`: identical wherever `{}`
    rendered, and rendering where `{}` raised. An earlier version had
    `__missing__` return `self`, which made a one-level dereference
    render the literal `{}` -- strictly worse than what it replaced, for
    the depth that is by far the most common.

    That combination is the point, and it is why this is NOT a
    `ChainableUndefined` subclass. That version chained correctly but
    stopped being a dict, which BROKE `{{ oidc|tojson }}` (a natural way
    to pass a whole blob, and an uncaught 500 in `create_compose`),
    turned `|int` into an UndefinedError, and rendered the literal text
    `Undefined` into the compose file for `|pprint`. Every one of those
    was a regression against the `{}` it replaced -- the fix was worse
    than the failure it prevented for any template that did not chain.

    Scoped deliberately to the compose render. `greffon_info` still holds
    a plain `{}`, so `_render_baked_file` keeps raising ConfigRenderError
    (422) on `{{ oidc.issuer }}` rather than writing a silently empty
    config file: a baked file is content, where quiet truncation is worse
    than a loud refusal, and it has no strip pass to remove the key.
    """

    def get(self, key, default=_OMITTED):
        """`dict.get`, except that a missing key with no default gives
        the forgiving Undefined rather than `None`.

        `{{ smtp.get('host') }}` otherwise rendered the literal string
        `None` into a container's environment -- a value that is neither
        empty nor correct, which is the whole class this object exists
        to prevent. A caller who passes an explicit default still gets
        it, so `.get(k, 'x')` is unchanged.
        """
        if key in self:
            return self[key]
        return self[key] if default is _OMITTED else default

    def __missing__(self, key):
        # A FIELD is undefined, not another empty mapping. Returning
        # `self` here made `{{ smtp.host }}` render the literal `{}`
        # where a plain `{}` binding rendered `''` -- a garbage value the
        # container then tries to use as a hostname, in place of the
        # benign empty one it used to get. That is a regression against
        # the type already shipping, and the docstring above claimed the
        # opposite of it.
        #
        # This is what makes attribute access arrive here at all, which
        # is not obvious: Jinja's `Environment.getattr` tries `getattr()`
        # and falls back to `getitem()`, so `{{ oidc.issuer }}` is a
        # missing KEY.
        # Named, not a shared no-name singleton. A few operations still
        # raise through `_UnsetField` -- `|int`, comparisons, arithmetic
        # -- and with no name the message was `None is undefined`, so a
        # 500 out of `create_compose` identified neither the type nor the
        # field. `main` named the field, so that was a diagnosability
        # regression on precisely the residual class this design accepts.
        # The allocation happens only on the rare path the strip pass
        # missed.
        # `obj=dict(self)`, not `self`. Jinja renders the object's type
        # into the message, so passing the wrapper leaked
        # `apps.utils.docker.compose._UnsetIntegration object` into a 500
        # an operator has to read. A plain dict restores main's wording,
        # `'dict object' has no attribute 'issuer'`, which is what they
        # would recognise.
        return _UnsetField(name=key, obj=dict(self))


def _is_integration_set(value):
    """Returns True iff the type's config payload is a non-empty dict.

    None, missing, and `{}` all map to "user didn't pick this integration"
    — we treat empty config the same as absence so the greffer doesn't
    render half-configured env vars (e.g. host without password) that
    would silently fail the underlying greffon's first SMTP attempt.
    """
    return isinstance(value, dict) and bool(value)


def _compute_integrations_context(greffon_info):
    """Lift each known integration type out of `greffon_info['integrations']`
    and into a top-level Jinja variable so catalog templates can reference
    e.g. `{{ smtp.host }}` directly.

    Unset types become empty dicts — Jinja's default Undefined resolves
    `{{ smtp.host }}` on `{}` to an empty string rather than blowing up
    with AttributeError on None.

    That `{}` is NOT belt-and-braces, whatever this docstring used to
    claim. `_compose_render_context` reads it as the signal to install
    the forgiving binding, and that binding is load-bearing: the strip
    pass cannot be made complete (aliasing and `|map(attribute=...)`
    move a dereference off the name entirely), so something has to
    survive what it misses. The two mechanisms divide as follows —
    the strip pass produces NO KEY, the binding produces an EMPTY VALUE.
    Only the strip pass can deliver the first, which is what glitchtip
    needs: an `EMAIL_URL` present but empty renders `smtp://:@:`, a
    malformed URL its app parses at boot.

    NOTE for the per-instance OIDC blobs of platform-identity Feature #3:
    `_is_integration_set` is `bool(dict)`, so a blob with ANY key reads
    as configured and switches BOTH mechanisms off. A half-written row —
    persisted before client registration finished, or a provider variant
    with a different shape — therefore reaches the render as a
    configured integration.

    What happens then depends on the SHAPE of the reference, and the
    quiet case is the likely one:

        {{ oidc.client_secret }}     ''   — Jinja's own default for a
                                            one-level miss. No error.
        {{ oidc.client_secret.x }}   UndefinedError
        {{ oidc.client_secret|int }} UndefinedError

    So a half-registered client does NOT reliably fail loudly: the
    commonest shape of all, reading a secret directly, deploys an empty
    string. This is identical to `main` and to any plain dict, so it is
    not a regression — but Feature #3 must not lean on a refusal that
    only covers chained access. Enforce the shape in the catalog
    validator, or have the manager refuse to send an incomplete blob.
    """
    integrations = greffon_info.get('integrations')
    # Shape-defensive for the same reason `_compute_config_context` is:
    # this runs eagerly for EVERY greffon at start, so a malformed
    # manager payload must not 500 a deploy that works today. A non-
    # mapping `integrations` is no integrations.
    if not isinstance(integrations, dict):
        integrations = {}
    for t in KNOWN_INTEGRATION_TYPES:
        value = integrations.get(t)
        # Always set the key so the Jinja context has a stable shape;
        # never overwrite a key the caller already populated (paranoia).
        greffon_info.setdefault(t, value if _is_integration_set(value) else {})
    return greffon_info


def _compute_config_context(greffon_info):
    """Expose per-instance config values under a ``config`` namespace so a
    render-flagged baked file can reference them, e.g.
    ``{{ config.OIDC_RP_CLIENT_SECRET }}`` in a Keycloak realm.

    We key by the env-destination ``key`` (not a catalog alias) so the file
    and the container provably read the SAME value: the env branch of
    ``apply_configuration`` and this context both read
    ``configuration['value'].get('value', '')``. Only configs that have an
    ``env`` destination are reachable; a file/json-only config contributes
    nothing here.

    Defensive against malformed payloads (a non-dict ``value``, a non-dict
    destination): this runs eagerly for EVERY greffon at start, so it must
    not 500 a deploy that works today.
    """
    config = {}
    for configuration in greffon_info.get('configurations', []) or []:
        value = configuration.get('value')
        if not isinstance(value, dict):
            continue
        for dest in configuration.get('destinations', []) or []:
            if isinstance(dest, dict) and dest.get('type') == 'env' and 'key' in dest:
                config[dest['key']] = value.get('value', '')
    greffon_info.setdefault('config', config)
    return greffon_info


def build_render_context(greffon_info):
    """Compute the full Jinja render context ONCE, up front, so both
    ``apply_configuration`` (which renders baked file contents) and
    ``create_compose`` (which renders the compose body) see the same
    ``instance_*`` / integration / ``config`` variables.

    All three sub-builders use ``setdefault`` and are idempotent, so
    ``create_compose`` calling the first two again is a harmless no-op and
    needs no change. This must run BEFORE ``apply_configuration`` because
    file destinations are written (and rendered) there, before
    ``create_compose`` runs.
    """
    greffon_info = _compute_instance_context(greffon_info)
    greffon_info = _compute_integrations_context(greffon_info)
    greffon_info = _compute_config_context(greffon_info)
    return greffon_info


def _render_baked_file(raw, greffon_info, dest_name):
    """Sandboxed strict-render of baked file content (str or bytes). Raises
    ConfigRenderError (-> HTTP 422) on a missing/typo'd variable, an SSTI/
    security violation, or non-UTF-8 bytes — so none of those leak as a 500 or
    a silently-wrong file. SecurityError is a TemplateError subclass (listed
    explicitly for clarity)."""
    try:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        return _FILE_RENDER_ENV.from_string(text).render(**greffon_info)
    except (UndefinedError, TemplateError, SecurityError, UnicodeDecodeError,
            TypeError, ValueError) as exc:
        # TypeError/ValueError: e.g. `{{ x | tojson }}` on an undefined x raises
        # "not JSON serializable" rather than UndefinedError — still a render
        # failure, so a clean 422, not a 500.
        # Log the offending variable name / reason, never the resolved secret.
        logger.error("baked-file render failed for %s: %s", dest_name, exc)
        raise ConfigRenderError(f"{dest_name}: {exc}") from exc


def _render_json_value(value, greffon_info, dest_name):
    """Render Jinja in the STRING LEAVES of a json-destination value (then the
    caller ``json.dumps`` the result). Rendering leaves rather than the
    serialized string means ``json.dumps`` escapes the substituted values, so a
    minted secret containing a quote or backslash can't corrupt the JSON."""
    if isinstance(value, str):
        return _render_baked_file(value, greffon_info, dest_name)
    if isinstance(value, dict):
        # Values only, not keys — templated keys are unneeded and would add a
        # duplicate-key failure mode. (Intentional.)
        return {k: _render_json_value(v, greffon_info, dest_name) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_json_value(v, greffon_info, dest_name) for v in value]
    return value


# Filters that read an attribute off their operand, so they dereference
# without producing a `Getattr` node. `attr` names the attribute
# positionally; the rest take an `attribute=` keyword.
# Slots whose occupant only DECIDES something: the mapping's contents
# never reach the output through them. An occurrence of the type inside
# one of these is a guard, not a read.
# These are every such slot Jinja has, not a sample: the classes with a
# `test` field are `If`, `CondExpr` and `For` (the `{% for x in xs if
# cond %}` filter), plus `Test.node`, the thing an `is` test examines.
# `For.test` was missed at first, and the cost shows why the list is
# derived rather than recalled -- `{% for x in ['off'] if not smtp %}`
# is the same guard as `{% if not smtp %}`, renders the same `off` on
# an unset integration, and was popped while the `{% if %}` spelling
# was kept.
_GUARD_SLOTS = frozenset({
    (nodes.If, 'test'),
    (nodes.CondExpr, 'test'),
    (nodes.For, 'test'),
    (nodes.Test, 'node'),
})


def _call_consumes(value, name):
    """Does a call in this subtree take the integration as an ARGUMENT?

    A guard is exempt because it only DECIDES -- the mapping's contents
    cannot reach the output through a test. Passing them INTO a call
    breaks that, because the call can stash them somewhere the value
    prints afterwards:

        {% set l=[] %}{% if l.append(smtp.host) %}{% endif %}{{ l[0] }}

    read `smtp.host` inside an `If.test` and printed it outside, so the
    guard exemption kept a value rendering `smtp://` in the glitchtip
    shape -- present and malformed.

    Arguments only, not the receiver. `{% if oidc.issuer.startswith("h") %}`
    calls a method ON the integration and uses the result to decide;
    nothing leaves the test. Treating every call as unsafe popped that,
    which is an ordinary catalog idiom and a working conditional.
    """
    for item in (value if isinstance(value, list) else [value]):
        if not isinstance(item, nodes.Node):
            continue
        calls = ([item] if isinstance(item, nodes.Call) else []) + list(
            item.find_all(nodes.Call))
        for call in calls:
            passed = list(call.args or []) + [
                kw.value for kw in (call.kwargs or [])]
            for extra in (call.dyn_args, call.dyn_kwargs):
                if extra is not None:
                    passed.append(extra)
            for arg in passed:
                if not isinstance(arg, nodes.Node):
                    continue
                if isinstance(arg, nodes.Name) and arg.name == name:
                    return True
                if any(n.name == name for n in arg.find_all(nodes.Name)):
                    return True
    return False


# The ONE environment the compose is rendered in -- by the document
# guard and by `create_compose` itself.
#
# ONE, because the guard's whole claim is "the question I ask is the
# question the render will ask". While these were two environments that
# claim was false in both directions, and both ways were 500s at
# `/start/` on composes `main` deploys: a value the guard's environment
# accepted but the deploy's refused was kept and then failed, and one
# the guard refused made the document look unrenderable, which silently
# disabled the net below.
#
# SANDBOXED, because catalog text is community-controlled and this
# module already sandboxes baked files against exactly this
# (`_FILE_RENDER_ENV`): a plain Environment executes
# `{{ cycler.__init__.__globals__.os.popen("id").read() }}` on a worker
# holding the instance's secrets, the manager token and the Docker
# socket. The strip pass made that worse by rendering values it exists
# to DELETE, before deleting them.
#
# Sandboxing the deploy render is a change to `main`'s behaviour, so it
# was measured, not assumed: all 32 catalog entries render BYTE
# IDENTICALLY sandboxed and unsandboxed, in both the configured and
# unset scenarios (64/64).
_COMPOSE_RENDER_ENV = SandboxedEnvironment()


# Operations that RAISE on `_UnsetField` instead of answering falsily.
# The guard exemption assumes a guard renders correctly when the
# integration is unset, and for bare truthiness and `==`/`!=` it does.
# It does not for these: `{{ "true" if smtp.port|int == 465 else
# "false" }}` raises UndefinedError, and `main` pops that key and
# deploys. Reproduced against main before this was written.
_COERCING_NODES = (
    nodes.Filter,
    nodes.Add, nodes.Sub, nodes.Mul, nodes.Div, nodes.FloorDiv,
    nodes.Mod, nodes.Pow, nodes.Neg, nodes.Pos,
)
# Comparisons that answer harmlessly on an unset field, checked by
# rendering each against the binding: `==`/`!=` give False. Ordering
# (`<`, `>`, `<=`, `>=`) genuinely coerces and is absent on purpose.
#
# `in`/`notin` were briefly listed here, to keep
# `{% if smtp.tls_mode in ["tls","starttls"] %}` -- a guard one word
# away from the eight shipping `== "starttls"` entries the exemption
# exists to protect. That was wrong: membership is safe only when the
# RIGHT operand is a container comparing by `==`. With the unset field
# on the left of a string or a number it raises, and the key is kept
# for the render to die on:
#
#   {% if smtp.tls_mode in "tls starttls" %}   TypeError: 'in <string>'
#   {% if smtp.port in 25 %}                   TypeError: not a container
#
# `main` pops both and deploys, so listing them turned an accepted
# over-pop into a 500. A narrower rule (exempt only when the right
# operand is a list/tuple/dict literal) would keep the bracket form,
# but no catalog entry writes a membership guard at all -- 0 across
# 399 YAMLs -- so the exemption would protect nothing that ships.
_SAFE_COMPARISON_OPS = frozenset({'eq', 'ne'})

# Tests that coerce their operand, found by enumerating every builtin
# test in `_COMPOSE_RENDER_ENV` against the unset binding. `nodes.Test`
# is a GUARD SLOT, so without naming these the withdrawal never saw
# them: `{% if smtp.port is gt(1) %}` kept its key and 500'd. Not the
# whole node type -- `is defined`, `is string`, `is mapping`, `is eq`
# and the rest answer harmlessly on an unset field, and killing the
# exemption for those would strip settings that work.
# `in` is here for the same reason it is absent from
# `_SAFE_COMPARISON_OPS`: `{% if smtp.tls_mode is in("starttls") %}` is
# the operator spelled as a TEST, so the Compare rule never sees it,
# and it raises the same TypeError.
#
# When re-deriving this set by enumeration, discard wrong-ARITY
# failures: probing `is callable("x")` raises "takes exactly one
# argument (2 given)", which looks like a coercion and is not --
# `is callable` renders fine on an unset field. The same trap once put
# `is even` on the wrong side of the line.
_COERCING_TESTS = frozenset({
    'gt', 'greaterthan', 'ge', 'lt', 'lessthan', 'le',
    'even', 'odd', 'divisibleby', 'in',
})


def _test_is_hazardous(node):
    """A `nodes.Test` that must NOT confer the guard exemption.

    Two ways a test breaks the exemption's promise that an unset guard
    renders its off-branch harmlessly:

    * it COERCES the operand (`is gt`, `is even`), which raises through
      `_UnsetField`; and
    * the environment does not HAVE it. Jinja resolves a test name at
      render time in a boolean slot, so `{% if smtp.host is nonempty %}`
      -- an Ansible spelling, or just `is defiend` -- parses cleanly,
      reads nothing, keeps its key, and then dies with
      TemplateRuntimeError. `main` keeps and dies on the `{% if %}`
      spelling too -- its pass 2 needs `{{` in the value -- so this is
      a 500 turned into an accepted over-pop, not a regression this
      branch introduced and then repaired.

    Consulted in BOTH places, which is the whole point of the helper:
    where the test sits in someone else's guard slot, and where its own
    `(Test, 'node')` slot is descended into. Naming it in only one of
    them lets the test re-exempt its own operand one level down.
    """
    return (isinstance(node, nodes.Test)
            and (node.name in _COERCING_TESTS
                 or node.name not in _COMPOSE_RENDER_ENV.tests))


def _calls_a_method_on(node, name):
    """Is this a method invoked on the integration MAPPING itself?

    `_UnsetIntegration` is a real dict, so `smtp.popitem()` raises
    KeyError, `smtp.get()` raises TypeError, `smtp.pop("host")` raises
    KeyError -- none of which is Undefined coercion, so the rules above
    never saw them. Worse than a 500: `{{ "" if smtp.update({"host":
    "x"}) else "" }}` MUTATES the binding, and a sibling
    `{% if smtp %}` in the same document then renders `on`, telling the
    greffon SMTP is configured when it is not.

    Only when the receiver is the mapping. A call on a FIELD --
    `{{ "a" if smtp.host.startswith("h") else "b" }}` -- stays exempt,
    because `_UnsetField.__call__` absorbs it and returns itself. That
    is the shape the catalog actually writes, and the exemption exists
    for it.
    """
    if not isinstance(node, nodes.Call):
        return False
    callee = node.node
    if isinstance(callee, nodes.Name):
        return callee.name == name
    # The `isinstance(..., nodes.Name)` narrowing is AttributeError
    # safety rather than a rule. From parseable source the only other
    # nodes that can sit here carrying a `.name` are `Filter` and
    # `Test`, and widening to them changes no strip decision -- but
    # NOT for the reason it is tempting to give. `_COERCING_NODES` does
    # not save the `Filter` case: a filter node named `smtp` contains no
    # `Name('smtp')`, so it fails `_guard_coerces`'s own `reaches`
    # check. What saves it is `_call_consumes`, which is tested first at
    # the single call site -- reaching the type through such a callee
    # requires `Name` inside the `Call`, which is either in the callee
    # subtree (so the Filter/Test reaches it and fires) or in the args
    # (so `_call_consumes` fires).
    return (isinstance(callee, nodes.Getattr)
            and isinstance(callee.node, nodes.Name)
            and callee.node.name == name)


def _guard_coerces(value, name):
    """Does this guard apply an operation to the type that RAISES unset?

    The exemption exists because a guard on an unset integration renders
    the "off" branch rather than failing -- true for `{% if smtp %}` and
    for `== "starttls"`, which is the shape eight shipping entries use.
    It is false the moment the guard coerces a FIELD: `|int`, `|round`,
    `|abs`, arithmetic, or an ordering comparison all raise through
    `_UnsetField`, and the key is then kept for the render to die on.

    Ordering, not equality: `==` and `!=` on an Undefined answer False,
    which is why the catalog's `== "starttls"` guards are safe and must
    stay exempt.
    """
    # `value` is whatever sits in the slot: a node, a list of them, or
    # None -- `For.test` is None whenever the loop has no `if` filter,
    # and iterating that raised, which `_dereferences` then swallowed
    # into "not a dereference" and kept every such key.
    candidates = value if isinstance(value, (list, tuple)) else [value]
    for node in candidates:
        if not isinstance(node, nodes.Node):
            continue
        for candidate in ([node] + list(node.find_all(nodes.Node))):
            reaches = any(
                n.name == name for n in candidate.find_all(nodes.Name))
            if not reaches:
                continue
            if isinstance(candidate, _COERCING_NODES):
                return True
            if (isinstance(candidate, nodes.Compare)
                    and any(op.op not in _SAFE_COMPARISON_OPS
                            for op in candidate.ops)):
                return True
            if _test_is_hazardous(candidate):
                return True
            if _calls_a_method_on(candidate, name):
                return True
    return False


# A Jinja block, including one left unterminated at the end of a value
# (`{{ smtp.host }` -- which is exactly the shape that breaks a
# document and sends everything down the fallback below).
_JINJA_BLOCK_RE = re.compile(r'\{\{.*?\}\}|\{%.*?%\}|\{\{.*$|\{%.*$', re.S)


def _named_in_a_jinja_block(value, name):
    r"""Does the type appear as a NAME inside a Jinja block?

    The fallback for everything the AST cannot answer, so its precision
    decides two failure directions at once.

    A bare `\bsmtp\b` over the whole value sprayed: it popped
    `{{ mail.smtp }}` (an attribute of something else) and
    `{{ instance_id }}-smtp` (plain text), dropping both working vars
    where `main` pops only the broken one and deploys them.

    `main`'s own narrow `smtp` + `.`/`[` test is too narrow the other
    way: it does not see `{{ dict(smtp).get("user") }}`, which reads the
    type through a wrapper and must be popped.

    So: inside a block, and preceded by neither a dot nor a word
    character -- the dot separates `smtp.host` and `dict(smtp)` from
    `mail.smtp`, and the `\w` half stops `{{ mysmtp }}` matching.
    """
    return any(
        re.search(rf'(?<![.\w]){re.escape(name)}\b', block) is not None
        for block in _JINJA_BLOCK_RE.findall(value))


def _reads(ast, name):
    """Does this parsed template READ the top-level `name`?

    Asked in two halves, and the split is the point.

    The BASE is `meta.find_undeclared_variables`, which is Jinja's own
    answer to "which variables does this template use". It is complete
    by construction: every occurrence, in every construct, present and
    future. Enumerating the constructs that read a variable was tried
    instead and was wrong ELEVEN times -- bare-`Name` targets only,
    `|attr()`, whole-mapping filters (`|urlencode`, `|items`),
    `namespace(v=smtp).v`, `dict(smtp).get()`, `{% for k in smtp %}`,
    `{{ smtp }}`, `{{ smtp ~ "" }}` -- each found by a different review,
    each an under-pop that shipped a malformed value. A list reached
    that way is evidence of nothing about the twelfth case.

    What is enumerated instead is the GUARD positions, and that
    inversion is what makes the failure direction safe. Miss a read
    construct and the base rule still catches it. Miss a guard slot and
    the cost is one env var over-popped on an integration nobody
    configured -- the trade this module makes everywhere.

    A guard renders CORRECTLY on an unset integration, because the
    binding is a falsy empty mapping: `{% if smtp %}on{% else %}off
    {% endif %}` gives `off`, and `{{ 'y' if smtp.host else 'n' }}`
    gives `n`. Popping those would discard a working setting.

    `|default` is NOT a guard, contrary to what this code claimed for
    two rounds. The binding is DEFINED, so `{{ smtp|default('x') }}`
    renders `{}` rather than `x` -- there is no author handling to
    preserve, only garbage to remove. (`default(x, true)` does fire, and
    is over-popped; that is the accepted direction.)

    Left incomplete on purpose: aliasing and dataflow.
    `{% set a = smtp %}{{ a.host }}` moves the read onto another name,
    and `find_undeclared_variables` reports `smtp` for it, so it is
    popped -- but a macro parameter carrying it across two env values is
    not something any single-value analysis can follow. The binding and
    the document guard cover that.
    """
    if name not in meta.find_undeclared_variables(ast):
        return False

    # Only real nodes are ever pushed, which is why there is no
    # empty-slot check here. The previous implementation walked a
    # `Filter`'s operand directly, and a filter BLOCK
    # (`{% filter upper %}..{% endfilter %}`) has that slot EMPTY, so it
    # raised an uncaught AttributeError out of `/start/`. Pushing only
    # `Node` instances makes that unrepresentable rather than guarded.
    # Three-valued walk. `guarded` says a guard above vouches for this
    # subtree; `sealed` says a withdrawal above it did not, and no slot
    # deeper down may vouch for it either.
    #
    # The seal is what makes a withdrawal stick. Guardedness alone is
    # monotone -- it can only go False -> True -- so refusing the
    # exemption at one level did nothing to stop the next level
    # granting it again. `(Test, 'node')` is itself a guard slot, so a
    # HARMLESS test nested inside a hazardous one re-granted what the
    # hazardous one had just been denied:
    #
    #     {% if smtp.host is nonempty %}                 popped
    #     {% if (smtp.host is defined) is nonempty %}    KEPT, and 500s
    #
    # Same for a `CondExpr` inside a hazardous test. The first spelling
    # was fixed and tested; the second differs only by a pair of
    # parentheses and died with the same TemplateRuntimeError.
    stack = [(ast, False, False)]
    while stack:
        node, guarded, sealed = stack.pop()
        if isinstance(node, nodes.Name) and node.name == name:
            if guarded:
                continue
            return True
        for field, value in node.iter_fields():
            is_slot = (type(node), field) in _GUARD_SLOTS
            # The test's NAME has to be consulted where its operand is
            # descended into, not only where the test sits in someone
            # else's slot.
            # `not guarded` restores a short-circuit the seal dropped.
            # The old expression was `guarded or (...)`, so once a guard
            # above vouched for this subtree Python stopped evaluating
            # the helpers. Recomputing them unconditionally took
            # `_test_is_hazardous` from O(n) to O(n^2) calls on nested
            # guards. It is behaviour-preserving: `child_guarded` is
            # `guarded or ...`, so guardedness never goes back to False
            # once set, no deeper Name is ever reported, and `sealed`
            # only ever feeds `child_guarded`.
            withdrawn = not guarded and is_slot and (
                _test_is_hazardous(node)
                or _call_consumes(value, name)
                or _guard_coerces(value, name))
            child_sealed = sealed or withdrawn
            child_guarded = guarded or (is_slot and not child_sealed)
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, nodes.Node):
                    stack.append((item, child_guarded, child_sealed))
    return False


def _why_it_will_not_render(compose, render_context):
    """The render failure as text, for the restore's log line.

    `_document_renders` answers yes/no and throws the exception away,
    which left an operator with "the strip broke it" and no cause. This
    re-runs the same render only to name the failure, on the path that
    has already decided to restore.
    """
    try:
        _COMPOSE_RENDER_ENV.from_string(yaml.dump(compose)).render(
            **copy.deepcopy(render_context))
    except Exception as exc:
        return '%s: %s' % (type(exc).__name__, exc)
    return 'no longer reproducible'


def _document_renders(compose, render_context):
    """Does the compose survive the WHOLE render once dumped?

    Dump, compile, render -- the three steps `create_compose` performs,
    with the same context, so the answer is the one that matters rather
    than an approximation of it. Compiling alone was not enough twice
    over: a family of `TemplateAssertionError`s comes only from the
    compile step, and a symbol can be DEFINED in one env value and USED
    in another, so popping the definition leaves a document that
    compiles and then raises `'u' is undefined`.

    The context is deep-copied PER CALL, and that is load-bearing.
    Copying it once at the call site and reusing it was not enough: a
    catalog expression that mutates the context corrupts the copy, and
    every later verdict then answers a different question than
    `create_compose` will ask with its own fresh context. Both
    directions shipped real bugs -- a mutation that removes made the
    guard undo every pop, so glitchtip's `EMAIL_URL` went out as
    `smtp://:@`; a mutation that adds made the guard approve a document
    that then raised at `/start/`.

    A context that will not copy means the question cannot be asked
    safely, so the answer is no.

    Any failure is a no, including a compose that will not dump.
    """
    try:
        context = copy.deepcopy(render_context)
    except Exception:
        return False
    try:
        _COMPOSE_RENDER_ENV.from_string(yaml.dump(compose)).render(**context)
    except Exception:
        return False
    return True


def _delete_unset_integration_env_keys(compose, greffon_info):
    """For each known integration type whose config is unset, pop every
    env key in the compose that would expand to an unset-integration
    Jinja reference. This aims at ``absent ⇒ no env var`` regardless of
    how Jinja renders ``{{ smtp.host }}`` on an empty dict — and,
    crucially, regardless of whether the catalog destination metadata
    actually reached the greffer for this start. See below for the one
    case where it settles for less.

    Two passes:

    1. **Metadata-driven**: walk ``greffon_info['configurations']``
       (the manager-sent destination list) and pop keys whose
       ``destination.type`` matches an unset integration. Works when
       the manager sent the full catalog destination set.

    2. **Template-driven** (fixes Nextcloud install on no-SMTP): walk
       every service's environment, parse each value with Jinja and pop
       any that READS an unset type. Not a text match: it sees
       ``{{ smtp }}``, ``{% for k in smtp %}`` and ``dict(smtp).get()``,
       and it deliberately KEEPS a value where the type only decides a
       branch (``{% if smtp %}``), which renders harmlessly when unset.
       Works even when the manager
       only sent user-submitted configurations (the historical shape):
       Nextcloud's ``MAIL_FROM_ADDRESS: '{{ smtp.from_address.split(...) }}'``
       has no per-instance value (user didn't pick SMTP, so the manager
       has nothing to send) so the metadata pass alone can't see it;
       the template pass catches it directly from the compose body
       before Jinja renders.

    The guarantee is "absent integration => no env var", with one
    documented exception: if the strip breaks the document, every pop is
    put back (see the restore at the end of this function), and those
    keys render EMPTY through the binding rather than being absent. That
    is the weaker outcome, taken only when the alternative is a document
    that will not render at all.

    SCOPE: both passes look only at ``services[*].environment``. A
    reference from ``command``, ``labels``, ``env_file`` or anywhere else
    is NOT popped, and the binding does not save every such case either
    (it absorbs attribute access, but ``{{ oidc.port|int }}`` still
    raises). Those render exactly as they do on ``main`` -- this function
    narrows the failure, it does not eliminate it. The guarantee is
    "absent integration => no env var", not "no reference anywhere".

    Defensive on shape: catalog metadata is supposed to use mapping-
    form ``environment:`` per the Feature #2 validator, but compose
    YAML also permits list form (``KEY=value``); both passes handle
    each form.
    """
    integrations = greffon_info.get('integrations')
    if not isinstance(integrations, dict):
        integrations = {}
    services = compose.get('services') if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        # `services:` with an empty body parses to None.
        services = {}

    unset_types = [
        t for t in KNOWN_INTEGRATION_TYPES
        if not _is_integration_set(integrations.get(t))
    ]

    # Every pop, from BOTH passes. The restore puts back pass 1's
    # metadata-driven pops as well, and the ERROR line naming the
    # restored keys is the only signal that an instance is quietly
    # degraded -- listing only pass 2's undercounted exactly what an
    # operator needs to see.
    popped = []
    if not unset_types:
        return compose

    # Whether the document renders is a property of the WHOLE document,
    # but both passes decide one value at a time, and a Jinja construct
    # can span two of them. Popping one half leaves the other dangling
    # and fails the entire render -- a 500 at `/start/`, far worse than
    # the env var the pop was worth.
    #
    # Reviews found this in shapes that argue against ever fixing it
    # value-by-value: `{% raw %}` PARSES alone (so it is kept) while its
    # `{% endraw %}` does not (so it is popped), and a `{# comment`
    # opener holds no Jinja delimiter at all (so it is never examined)
    # while the `{{ oidc.host }} #}` closing it is a genuine reference
    # that must be popped. No per-value rule is right about both.
    #
    # So stop enumerating shapes and check the invariant instead: if the
    # document was usable before the strip and is not after, the pops
    # that broke it are undone. Costs one extra parse of a document we
    # are about to render anyway, and catches shapes nobody predicted --
    # including that popping a `{% raw %}` pair UN-SHIELDS what sat
    # between it, so the strip can add constructs, not only remove
    # them.
    # The exact context `create_compose` renders with, so the guard's
    # question is the render's question and not a proxy for it.
    # `_document_renders` copies it per call -- see there for why once
    # at this call site was not enough.
    render_context = _compose_render_context(greffon_info)
    rendered_before = _document_renders(compose, render_context)
    snapshot = None
    if rendered_before:
        try:
            snapshot = copy.deepcopy(compose)
        except Exception:
            # No snapshot means no undo; the guard below keys on
            # `snapshot is not None`. A compose holding something
            # uncopyable (a module dumps but does not deepcopy) is not
            # the shape this defends, and raising here would be the very
            # 500 it exists to avoid.
            pass

    # Pass 1 — metadata-driven pop (unchanged behavior).
    for t in unset_types:
        configurations = greffon_info.get('configurations')
        if not isinstance(configurations, list):
            configurations = []
        for configuration in configurations:
            if not isinstance(configuration, dict):
                continue
            destinations = configuration.get('destinations')
            if not isinstance(destinations, list):
                continue
            for destination in destinations:
                if not isinstance(destination, dict):
                    continue
                if destination.get('type') != t:
                    continue
                container = destination.get('container')
                key = destination.get('key')
                # `isinstance(str)`, not just truthiness: catalog
                # metadata is JSON, so either of these can arrive as a
                # list, and an unhashable dict lookup raises TypeError
                # straight out of `/start/`.
                if not isinstance(container, str) or not isinstance(key, str):
                    continue
                if not container or not key:
                    continue
                service = services.get(container)
                if not isinstance(service, dict):
                    continue
                env = service.get('environment')
                if isinstance(env, dict):
                    if key in env:
                        popped.append('%s.%s' % (container, key))
                    env.pop(key, None)
                elif isinstance(env, list):
                    # Read the entry the way compose does: split on the
                    # first `=` and take the NAME. Matching only
                    # `KEY=` left a bare `KEY` behind, which is legal
                    # compose meaning "import this variable from the
                    # host" -- so for an integration the user never
                    # configured, the container got whatever the GREFFER
                    # HOST has under that name. Worse than the failure
                    # this pass exists to stop: not a missing var, but
                    # someone else's.
                    #
                    # The `.strip()` covers ` KEY`, `KEY ` and
                    # `KEY =x`. Those are malformed compose -- a name
                    # with a space is not a name the host will have --
                    # so this closes no live hole; it is here so the
                    # rule matches compose's own parsing rather than
                    # relying on the spelling being unusable. It stays
                    # exact on the name, so `OIDC_CLIENT_IDENTITY` and
                    # `OIDC_CLIENT_IDX=1` survive.
                    kept_entries = [
                        e for e in env
                        if not (isinstance(e, str)
                                and e.split('=', 1)[0].strip() == key)
                    ]
                    if len(kept_entries) != len(env):
                        popped.append('%s.%s' % (container, key))
                    service['environment'] = kept_entries

    # Pass 2 -- template-driven pop, for values the catalog templates
    # rather than declares.
    #
    # ASKS JINJA'S OWN PARSER rather than approximating it. Five
    # hand-rolled scanners preceded this and each leaked somewhere a
    # regex approximates a grammar: string literals holding delimiters,
    # statement blocks, spaced dots, nested mapping braces, call parens,
    # and finally a normalisation that backtracked quadratically on a
    # whitespace run -- a denial of service on catalog-controlled input
    # reached through `/start/`. The parser does not approximate; string
    # literals, comments, `{% raw %}`, whitespace and nesting are its
    # job. See the commit history for which scanner failed how.
    #
    # A reference is any USE of the type that is not a guard. Which
    # uses exist is Jinja's answer, not ours -- see `_reads`, and the
    # eleven constructs that had to be added one at a time before the
    # question was asked that way round.
    parser_env = Environment()

    def _dereferences(value, name):
        """Does `value` read the top-level `name` and dereference it?

        Asks Jinja's PARSER. A dereference is a `Getattr`/`Getitem` whose
        target is a `Name` for the type -- which is what the question
        literally is, so there is nothing left to approximate.

        Hand-written scanners preceded it and were repeatedly wrong
        about string literals holding delimiters, nesting, and
        call-versus-grouping parens. The parser has already resolved
        all of that.

        Still incomplete in one place, and it is a dataflow one rather
        than a syntactic one: aliasing (`{% set a = oidc %}{{ a.issuer }}`,
        a macro argument) moves the dereference onto another name
        entirely, and no amount of looking at THIS expression can follow
        it.

        The binding covers the SIMPLE aliases -- `{{ base }}` where
        `base` was set from `oidc.url` renders empty rather than
        raising. It does NOT cover a name bound to a definition that
        gets popped: the binding knows `oidc`, not the macro named after
        it. `{% macro u() %}{{ oidc.x }}{% endmacro %}` in one env value
        and `{{ u() }}` in another raises `'u' is undefined`. That is
        what the document guard is for.

        Handles a locally shadowed name correctly, which is worth
        stating because it used to be listed here as an accepted
        over-pop. `{% for smtp in xs %}{{ smtp.a }}{% endfor %}` binds
        the name locally and never touches the integration, so the key
        survives -- `find_undeclared_variables` gets the scoping right
        and does not report it. `main` pops it.
        """
        if not document_parses:
            # `main`'s shape, not a bare word match: a broken document
            # is often repaired by exactly the pop `main` would make,
            # and a bare `\bsmtp\b` additionally sprayed over
            # `{{ mail.smtp }}` and `{{ instance_id }}-smtp`, dropping
            # both working vars where `main` deploys them.
            # The document is already broken, so nothing is being
            # protected: the deploy fails as it stands, popping cannot
            # break a working document, and popping may well FIX it --
            # which is what `main` does here, with the same crude
            # question. Applied to every value, not only the ones that
            # fail to parse alone: a value can parse perfectly and still
            # be what breaks the document (`{% raw %}` left open at the
            # end of it), or be the half that a straddle needs.
            return _named_in_a_jinja_block(value, name)
        try:
            parsed = parser_env.parse(value)
        except Exception:
            # The value does not PARSE. In a document that does, it is a
            # fragment of a construct spanning two env values, and
            # popping one half breaks the whole -- so keep it, as `main`
            # keeps it. (A document that does not parse returned above.)
            return False
        try:
            return _reads(parsed, name)
        except Exception:
            # Deliberately bare: the body is a single `parse()` on
            # catalog-controlled input, so there is no logic of ours for
            # a broad catch to hide, and anything escaping is a 500 at
            # `/start/`. Naming the classes was tried and failed four
            # times -- the last, `SyntaxError` from CPython via Jinja's
            # number lexer on `{{ 0.１ }}`, was found by fuzzing after
            # the third fix shipped. `Exception`, not `BaseException`,
            # so a KeyboardInterrupt still stops the process.
            #
            # This arm is the THIRD case, and only it: the value
            # parsed, the document parses, and the ANALYSIS failed.
            # (A broken document returned at the top; a value that
            # cannot parse inside a parsing document returned just
            # above.) Fall back to the same textual question those arms
            # ask -- name inside a Jinja block -- rather than to "the
            # name occurs at all", which is the bare `\bsmtp\b` rule
            # `_named_in_a_jinja_block` exists to replace.
            #
            # Reached because `find_undeclared_variables` COMPILES, so
            # an unknown filter (`{{ smtp.host|to_json }}`, the Ansible
            # spelling) raises `TemplateAssertionError` even though the
            # value parsed. Answering "not a reference" there kept a
            # value that plainly dereferences the unset type, and 500'd
            # where `main` pops it and deploys.
            #
            # A test Jinja does not have raises here too, but only in
            # OUTPUT position (`{{ smtp.host is nonempty }}`). In a
            # GUARD it resolves at render instead, so this arm never
            # sees it and `_guard_coerces` withdraws the exemption
            # explicitly.
            return _named_in_a_jinja_block(value, name)

    def _strings_in(value, seen=None):
        """Every string inside `value`, however nested.

        A compose env value is usually a scalar, but YAML permits a list
        or a mapping, and `yaml.dump` turns those into template text
        just the same. Skipping non-strings meant `A: ['{{ smtp.host }}']`
        was never examined and rendered `['']` -- present but empty, the
        failure this pass exists to stop.

        Each CONTAINER is visited once, by identity. YAML anchors make
        one object reachable by many paths, so a compact document builds
        a shared tree that this walk otherwise re-descends once per
        path: measured at 0.9s for depth 8, 3.4s for 12 and 80s for 16,
        doubling with every extra level, from a file small enough to
        sit in a catalog entry. `RecursionError` does not save it --
        the depth stays tiny, only the path COUNT explodes -- so
        `/start/` just hangs.

        Visiting a shared subtree once is enough for the question being
        asked. This yields strings only to decide whether ANY of them
        dereferences an unset type, and a second visit to the same
        object cannot change that answer.
        """
        if isinstance(value, str):
            yield value
            return
        if not isinstance(value, (dict, list, tuple)):
            return
        if seen is None:
            seen = set()
        if id(value) in seen:
            return
        seen.add(id(value))
        nested_values = value.values() if isinstance(value, dict) else value
        for nested in nested_values:
            yield from _strings_in(nested, seen)

    # Whether the DOCUMENT is a valid template, which is what decides
    # how to read a value that will not parse on its own. See
    # `_dereferences`.
    try:
        _COMPOSE_RENDER_ENV.parse(yaml.dump(compose))
        document_parses = True
    except Exception:
        document_parses = False

    def matching_unset_types(value):
        """Which unset types `value` dereferences, in tuple order."""
        try:
            # The delimiter check is a PERFORMANCE filter, not a
            # semantic one, and no test can pin it: every path out of
            # `_dereferences` needs a Jinja delimiter to answer yes --
            # `_named_in_a_jinja_block` matches only alternatives that
            # open with one, and `find_undeclared_variables` on
            # delimiter-free text returns an empty set. Dropping it
            # changes no answer, only how many values get parsed.
            texts = [
                text for text in _strings_in(value)
                if '{{' in text or '{%' in text
            ]
        except RecursionError:
            # A YAML alias can make an env value refer to itself
            # (`A: &a [..., *a]`), and `safe_load` accepts it. Walking
            # that never terminates. `main` renders such a compose, so
            # raising here would turn a working deploy into a 500 --
            # pop instead, which is the same answer this function gives
            # for anything else it cannot read.
            return tuple(unset_types)
        if not texts:
            return ()
        return tuple(
            t for t in unset_types
            if any(_dereferences(text, t) for text in texts)
        )


    def _log_pop(key, name, matched):
        # The types that MATCHED, not every unset type: on a fresh
        # instance both smtp and oidc are unset, so naming the whole set
        # would report a key that references only oidc as
        # "unset type: smtp, oidc" and make the field noise.
        popped.append('%s.%s' % (name, key))
        logger.info(
            'integrations: dropping env %s from service %s '
            '(references unset type: %s)',
            key, name, ', '.join(matched),
        )

    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        env = service.get('environment')
        if isinstance(env, dict):
            for key, value in list(env.items()):
                matched = matching_unset_types(value)
                if matched:
                    _log_pop(key, name, matched)
                    env.pop(key, None)
        elif isinstance(env, list):
            kept = []
            for e in env:
                matched = (
                    matching_unset_types(e.split('=', 1)[1])
                    if isinstance(e, str) and '=' in e
                    else ()
                )
                if matched:
                    _log_pop(e.split('=', 1)[0], name, matched)
                    continue
                kept.append(e)
            service['environment'] = kept

    # The strip must not make things WORSE. If the document rendered
    # before and does not now, the pops are the only thing that changed,
    # so put them all back and let the deploy fail the way it would have
    # without this pass -- loudly, and identically to `main`.
    #
    # Wholesale, not key by key. A chunked undo that kept every pop it
    # could was tried: ~210 lines, a wall-clock budget that made the
    # deployed file depend on how loaded the node was, and zero firings
    # across the whole catalog. Restoring everything is the same answer
    # for every input that exists, and a comprehensible one for those
    # that do not.
    if snapshot is not None and not _document_renders(compose, render_context):
        # The restore SUCCEEDS, so the deploy then works -- with those
        # keys present and empty instead of absent. This line is the
        # only signal that the instance is quietly degraded, so it
        # carries the cause and the keys, not just the id.
        logger.error(
            'integrations: the env strip broke the compose document for '
            '%s (%s); restoring %s, which will render empty rather than '
            'being absent',
            greffon_info.get('id'),
            _why_it_will_not_render(compose, render_context),
            ', '.join(popped) or 'nothing',
        )
        # `clear()` before `update()` is belt-and-braces and no test
        # can pin it: the strip only ever pops keys from a service's
        # `environment`, so the snapshot and the live document always
        # have the same top-level keys and `update` alone restores
        # completely. It stays so a future pass that adds a top-level
        # key cannot leave it behind on the rescue path.
        compose.clear()
        compose.update(snapshot)
    return compose


def _compose_render_context(greffon_info):
    """`greffon_info` with UNSET integration types bound so nothing raises.

    Unset only, deliberately. Wrapping configured types as well made a
    missing FIELD on a configured integration render empty, where `main`
    raises and names it:

        {{ smtp.from_addres.split("@")[1] }}   # renamed or mistyped
            main    UndefinedError: 'dict object' has no attribute ...
            wrapped ''

    `nextcloud` ships `{{ smtp.from_address.split("@")[1] }}` today, so
    that turned a loud refusal into `MAIL_DOMAIN=""` on every instance
    with SMTP configured. It also contradicted the rule this module
    applies to baked files -- quiet truncation is worse than a loud
    refusal -- by exempting the compose path from it.

    The cost is that a PARTIALLY populated blob raises on a CHAINED
    dereference of a field it does not carry. Only chained: reading the
    field directly still renders `''`, exactly as a plain dict does, so
    this is not a general "loud refusal" and Feature #3 must not treat
    it as one (see `_compute_integrations_context`). Where it does fire,
    it is the intended answer: a catalog entry reading an optional field
    should say so with `|default`, which is explicit and reviewable,
    rather than relying on the platform to paper over it.

    A caller that deliberately populated the key still wins, which is the
    same non-clobbering rule `_compute_integrations_context` follows.
    """
    context = dict(greffon_info)
    for t in KNOWN_INTEGRATION_TYPES:
        # Falsiness is the whole policy, and does two jobs at once.
        # `_compute_integrations_context` writes `{}` for an unset type
        # and the blob itself for a configured one, so this wraps unset
        # types ONLY -- a configured blob is a non-empty dict, hence
        # truthy, hence untouched. Re-deriving "unset" from
        # `integrations` here would be a second, redundant spelling of
        # the same condition that could drift from it.
        #
        # Falsy rather than `== {}` so that a `greffon_info` which
        # already carries a top-level `oidc: None` is bound too.
        # `_compute_integrations_context` uses `setdefault`, so such a
        # key is never normalised to `{}`, and leaving it would let
        # `{{ oidc.a.b }}` raise. A caller with a REAL preset still
        # wins, because a usable blob is truthy.
        if not context.get(t):
            context[t] = _UnsetIntegration()
    return context


def _compute_instance_context(greffon_info):
    """Expose ``instance_url`` / ``instance_host`` / ``instance_port`` /
    ``instance_id`` to the Jinja render context for catalog metadata
    templating.

    ``instance_url`` is the source of truth — it carries the URL the
    manager rendered for the first port (``ports[0].url`` — the
    wildcard subdomain ``https://<field-id>.my.<domain>``). That's
    what users hit in the browser and what greffons should bake into
    emails / OAuth redirects / share links.

    ``instance_host`` / ``instance_port`` are parsed-out convenience
    vars derived from ``instance_url``. They're kept for back-compat
    with catalogs that pre-date the manager-URL contract (the older
    shape exposed greffer-local ``GREFFER_PUBLIC_HOST`` / ``port_host``
    values). New catalogs should prefer ``instance_url`` + standard
    Jinja string ops at the call site (e.g.
    ``{{ instance_url.split('://')[1] }}`` for ``host[:port]``); the
    single source-of-truth variable avoids the cross-PR-contract
    burden of pre-parsed pieces.

    Falls back to a greffer-direct URL built from
    ``GREFFER_PUBLIC_HOST`` + ``port_host`` only when the manager
    didn't supply a URL (dev / test paths with no public proxy in
    front). Malformed or non-string manager values trigger the same
    fallback.

    Important semantics: when the manager-supplied URL has no
    explicit port (TLS default 443 — the wildcard-subdomain case),
    ``instance_port`` is the EMPTY STRING, not a fallback to
    ``port_host``. Catalogs that previously rendered
    ``host.docker.internal:51019`` (greffer-local) into user-facing
    env vars (Nextcloud OVERWRITEHOST, Plausible callback URLs,
    etc.) silently shipped broken values; the corrected semantics
    surface the actual user-facing port (empty for default 443,
    explicit for non-default).
    """
    ports = greffon_info.get('ports') or []
    # instance_url / instance_host / instance_port describe the Tier-A WEB entry
    # point, so pick the first non-L4 (nginx-proxied) port — never an L4 port,
    # whose public endpoint is a raw host:port carried by instance_l4_* instead.
    # A mixed greffon (e.g. a web UI + a raw UDP media/VPN port) would otherwise
    # leak the L4 subdomain into instance_url if the L4 port sorts first. A
    # purely-L4 greffon falls back to ports[0].
    first_port = next(
        (p for p in ports
         if isinstance(p, dict) and p.get('exposure_tier', 'http') != 'l4'),
        ports[0] if ports and isinstance(ports[0], dict) else {},
    )
    raw = first_port.get('url')
    port_host = first_port.get('port_host') or ''
    scheme = os.getenv('GREFFER_PUBLIC_SCHEME', 'https')
    fallback_host = os.getenv('GREFFER_PUBLIC_HOST', 'host.docker.internal')

    parsed = None
    parsed_port = None
    if isinstance(raw, str) and (raw.startswith('https://') or raw.startswith('http://')):
        try:
            parsed = urlparse(raw)
            # ``parsed.port`` is a property that re-parses netloc and
            # raises ValueError on a non-int port; wrap specifically.
            parsed_port = parsed.port
        except (ValueError, TypeError):
            parsed = None
            parsed_port = None

    # ``urlparse('abc')`` does NOT raise — it returns a ParseResult
    # with empty scheme/hostname. Treat half-parsed values as invalid
    # so we fall back to the greffer-local defaults instead of leaking
    # a malformed URL into ``instance_url``.
    manager_url_valid = (
        parsed is not None
        and bool(parsed.scheme)
        and bool(parsed.hostname)
    )

    if manager_url_valid:
        instance_host = parsed.hostname
        # Empty when the URL omits an explicit port (default 443) —
        # NOT a fallback to greffer-local port_host. Catalogs that
        # need a host:port form should use inline string ops on
        # ``instance_url`` (e.g. ``{{ instance_url.split('://')[1] }}``)
        # rather than concatenating these pieces; the catalog stays
        # correct regardless of whether the user-facing URL has an
        # explicit port. See greffon-catalog#15 for the Nextcloud
        # TRUSTED_DOMAINS migration.
        instance_port = str(parsed_port) if parsed_port else ''
        instance_url = raw
    else:
        # Greffer-direct fallback. Used by unit tests + dev paths
        # where no public proxy fronts the greffer. Here
        # ``port_host`` IS the user-facing port (the user reaches
        # the instance at ``<fallback_host>:<port_host>`` directly).
        instance_host = fallback_host
        instance_port = port_host
        instance_url = (
            f"{scheme}://{instance_host}:{instance_port}"
            if instance_port else f"{scheme}://{instance_host}"
        )

    greffon_info.setdefault('instance_id', greffon_info.get('id', ''))
    greffon_info.setdefault('instance_host', instance_host)
    greffon_info.setdefault('instance_port', instance_port)
    greffon_info.setdefault('instance_url', instance_url)

    # L4 (Tier-C) endpoint vars for catalog templating. An L4 app needs the
    # PUBLIC host:port its clients dial (e.g. WireGuard's WG_HOST / WG_PORT),
    # which is NOT a Tier-A https URL, so {{ instance_url }} can't express it.
    # In PROXY mode the greffer knows the endpoint at render time:
    # GREFFER_PUBLIC_HOST + the allocated host port. In TUNNEL mode the public
    # endpoint is RATHOLE_PUBLIC_HOST:tunnel_port, allocated manager-side AFTER
    # the greffer responds, so it is not knowable here (tunnel-mode UDP is gated
    # to phase 2); the vars are left empty. Always set (even empty) so
    # {{ instance_l4_* }} renders blank instead of erroring.
    l4_first = next(
        (p for p in ports
         if isinstance(p, dict) and p.get('exposure_tier') == 'l4'),
        None,
    )
    if l4_first is not None and greffon_info.get('l4_bind_host') != '127.0.0.1':
        # Proxy-mode L4 endpoint (the bind-host gate above already means
        # proxy, independent of GREFFER_MODE which is often unset for the
        # default proxy mode). The public host clients dial is the explicit
        # GREFFER_PUBLIC_HOST, else the manager-callback GREFFER_ADDRESS —
        # control plane and data plane share one host in the common single-IP
        # deployment. Never host.docker.internal, which is unreachable by
        # external clients and would break e.g. WireGuard peer configs.
        l4_host = (
            os.getenv('GREFFER_PUBLIC_HOST')
            or os.getenv('GREFFER_ADDRESS')
            or 'host.docker.internal'
        )
        l4_port = str(l4_first.get('port_host') or '')
        greffon_info.setdefault('instance_l4_host', l4_host)
        greffon_info.setdefault('instance_l4_port', l4_port)
        greffon_info.setdefault(
            'instance_l4_endpoint',
            f'{l4_host}:{l4_port}' if l4_port else l4_host)
        greffon_info.setdefault('instance_l4_proto', l4_first.get('protocol', 'tcp'))
    else:
        greffon_info.setdefault('instance_l4_host', '')
        greffon_info.setdefault('instance_l4_port', '')
        greffon_info.setdefault('instance_l4_endpoint', '')
        greffon_info.setdefault('instance_l4_proto', '')
    return greffon_info


def _inject_instance_log_rotation(compose):
    """Cap per-container log disk on the greffon INSTANCE containers
    (greffer-observability epic, Feature #3). Docker's json-file driver does
    NOT rotate by default, so a chatty instance can fill the operator's disk.
    Set ``max-size``/``max-file`` on every service that has not already declared
    its own ``logging`` (a catalog author's explicit choice wins). Values come
    from GREFFER_INSTANCE_LOG_MAX_SIZE / _MAX_FILE (read via os.getenv here,
    matching this module's env style; the same vars bind Settings fields)."""
    services = (compose or {}).get('services')
    if not isinstance(services, dict):
        return
    max_size = os.getenv('GREFFER_INSTANCE_LOG_MAX_SIZE', '10m')
    # ``max-file`` must be an integer string; coerce with a fallback so a
    # malformed GREFFER_INSTANCE_LOG_MAX_FILE does not write an invalid value
    # into EVERY rendered instance compose and break every greffon start.
    raw_max_file = os.getenv('GREFFER_INSTANCE_LOG_MAX_FILE', '3')
    try:
        max_file = str(int(raw_max_file))
    except (TypeError, ValueError):
        logger.warning(
            'invalid GREFFER_INSTANCE_LOG_MAX_FILE=%r; using default 3',
            raw_max_file)
        max_file = '3'
    for service in services.values():
        if not isinstance(service, dict) or 'logging' in service:
            continue
        service['logging'] = {
            'driver': 'json-file',
            'options': {'max-size': max_size, 'max-file': str(max_file)},
        }


def create_compose(compose, greffon_info):
    greffon_path = os.path.join(os.getenv('GREFFON_PATH', '/data'), greffon_info['id'])
    if not os.path.exists(greffon_path):
        os.makedirs(greffon_path)
    greffon_info = _compute_instance_context(greffon_info)
    # Feature #4: bring per-type integration configs into the Jinja
    # context BEFORE rendering, and strip catalog-declared env keys for
    # any integration type the user didn't pick. Order matters — the
    # delete pass runs against the post-template-mutation compose dict
    # but BEFORE Jinja substitution; it pops the SMTP env keys whose
    # values would otherwise be templated `{{ smtp.host }}` strings.
    greffon_info = _compute_integrations_context(greffon_info)
    # Log rotation FIRST: the strip's document guard approves the exact
    # text that will be rendered, and injecting afterwards changed that
    # text. Appending content is not neutral to Jinja -- an unclosed
    # `{% raw %}` is accepted at EOF and rejected once anything follows
    # it -- so the guard could pass on a document that then failed to
    # compile. Neither function reads what the other writes (one touches
    # `environment`, the other `logging`), so the order is free.
    _inject_instance_log_rotation(compose)
    _delete_unset_integration_env_keys(compose, greffon_info)
    t = _COMPOSE_RENDER_ENV.from_string(yaml.dump(compose))
    compose_file = t.render(**_compose_render_context(greffon_info))
    with open(os.path.join(greffon_path, 'docker-compose.yml'), 'w') as temp_file:
        temp_file.write(compose_file)

def remove_compose_file(greffon_info):
    greffon_path = get_greffon_path(greffon_info)
    template_path = os.path.join(greffon_path, 'docker-compose.template.yml')
    compose_path = os.path.join(greffon_path, 'docker-compose.yml')
    if os.path.exists(template_path):
        os.remove(template_path)
    if os.path.exists(compose_path):
        os.remove(compose_path)

def create_volumes_then_copy_files(greffon_info):
    for _, volume in greffon_info.get('volumes', {}).items():
        if not docker_is_volume_exist(volume):
            docker_create_volume(volume)
        docker_copy_file_into_volume(volume)

def apply_configuration(greffon_info, compose):
    for configuration in greffon_info.get('configurations', []):
        for destination in configuration.get('destinations', []):
            if destination['type'] == 'json':
                file_path = os.path.join(get_greffon_path(greffon_info), destination['name'])
                value = configuration['value']
                if destination.get('x-greffon-render'):
                    # Render the value's string leaves, THEN serialize, so
                    # json.dumps escapes substituted values (no corrupt JSON).
                    value = _render_json_value(value, greffon_info, destination['name'])
                text = json.dumps(value)
                with open(file_path, "w") as f:   # Opens file and casts as f
                    f.write(text)
                greffon_info['volumes'][destination['volume']]['files'].append({
                            'type': 'path',
                            'src': file_path,
                            'dest': destination['name'],
                        })
            elif destination['type'] == 'env':
                remove_compose_file(greffon_info)
                compose['services'][destination['container']].setdefault('environment', [])
                if isinstance(compose['services'][destination['container']]['environment'], dict):
                    compose['services'][destination['container']]['environment'][destination['key']] = configuration['value'].get('value', '')
                else:
                    compose['services'][destination['container']]['environment'].append(f'{destination["key"]}={configuration["value"].get("value", "")}')
            elif destination['type'] == 'file':
                remove_compose_file(greffon_info)
                file_path = os.path.join(get_greffon_path(greffon_info), destination['name'])
                # ``DataURI.data`` is ``bytes`` for base64 data-URIs but ``str``
                # for percent-encoded ones — normalize before writing/rendering.
                raw = DataURI(configuration['value']['file']).data
                if destination.get('x-greffon-render'):
                    # _render_baked_file decodes (and turns non-UTF-8 into a
                    # clean 422) — pass raw str/bytes straight through.
                    data = _render_baked_file(raw, greffon_info, destination['name']).encode('utf-8')
                else:
                    data = raw if isinstance(raw, bytes) else raw.encode('utf-8')
                    # Rollout self-warn: a verbatim file that still carries Jinja
                    # markers is almost certainly a render-flag/greffer-version
                    # mismatch (catalog expects rendering this greffer didn't do).
                    if b'{{' in data or b'{%' in data:
                        logger.warning(
                            "file destination %s contains Jinja markers but is not "
                            "x-greffon-render; writing verbatim (flag/version mismatch?)",
                            destination['name'],
                        )
                with open(file_path, "wb") as f:   # Opens file and casts as f
                    f.write(data)
                greffon_info['volumes'][destination['volume']]['files'].append({
                            'type': 'path',
                            'src': file_path,
                            'dest': destination['name'],
                        })
    return greffon_info


# docker-compose `${VAR}` interpolation resolves against the launching
# process's environment. The greffer process env holds its manager token
# (GREFFER_TOKEN) and other greffer-only config; the catalog is community-
# extensible and assumed hostile (see the sandboxed Jinja note above), so a
# malicious catalog compose with a literal ``${GREFFER_TOKEN}`` could otherwise
# exfiltrate the greffer's token into a tenant container. We therefore launch
# docker-compose with a SCRUBBED env: only what the CLI needs to find its
# binaries and reach the Docker daemon. Tenant config values are baked into the
# rendered compose by Jinja (create_compose), NOT via docker-compose ${}
# interpolation, so nothing legitimate depends on the greffer env here.
_COMPOSE_ENV_ALLOWLIST = (
    'PATH', 'HOME',
    # Daemon reachability (a socket-mounted greffer needs none of these, but a
    # TLS/remote daemon does, so pass them through when present).
    'DOCKER_HOST', 'DOCKER_CONFIG', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY',
)


def compose_env():
    """Minimal env for the docker-compose child: structurally prevents
    ``${GREFFER_TOKEN}`` (or any other greffer-only secret) from being
    interpolated by a hostile catalog compose.

    Public because EVERY ``docker-compose`` invocation in this codebase must use
    it, including the ones in ``app/backup.py``. Those two landed after the fix
    that introduced this and inherited ``os.environ`` for months -- see the
    call-site test in tests/test_compose.py, which now asserts the invariant
    across the whole tree rather than testing this function in isolation."""
    return {k: v for k, v in os.environ.items() if k in _COMPOSE_ENV_ALLOWLIST}


# Back-compat alias for the existing private call sites in this module.
_compose_env = compose_env


def start(greffon_info):
    """Bring the instance up (resource-monitoring epic, Feature 2 changes).

    Two changes over the original fire-and-forget ``up``:

    1. ``-p <instance_id>`` pins the compose project name to the instance id as
       an ENFORCED invariant (rather than relying on the v2 compose-file-dir-
       basename derivation), so the strict per-instance enumeration label
       ``com.docker.compose.project=<id>`` is exact by construction and immune
       to a binary/version/cwd shift. ``stop`` passes the same ``-p`` so the
       two never desync (a desync would let ``stop`` target a different project
       and stop nothing).
    2. ``up -d`` (detached) + stdout/stderr captured to a per-instance
       ``deploy.log``. Detaching makes the capture naturally pull/create-bounded
       (the launcher exits after create, so the file stops growing) and removes
       the lingering attached compose client coupled to the greffer's lifecycle.
       ``deploy.log`` is the only log available while an instance is pulling or
       after a failed deploy (no container exists yet to read). ``deploy.log``
       can echo registry credentials / pull errors, so it is surfaced only via
       the LOG_SURFACING-gated logs endpoint, never unconditionally.
    """
    path = get_greffon_path(greffon_info)
    compose_file = os.path.join(path, 'docker-compose.yml')
    # 'wb': each deploy truncates the previous deploy.log. The child process
    # inherits the fd; closing the parent's handle here is correct (the child
    # keeps writing until 'up -d' returns after create, then the OS closes it).
    deploy_log = open(os.path.join(path, 'deploy.log'), 'wb')
    try:
        return subprocess.Popen(
            ['docker-compose', '-p', greffon_info['id'], '-f', compose_file,
             'up', '-d'],
            stdout=deploy_log, stderr=subprocess.STDOUT,
            env=_compose_env(),
        )
    finally:
        deploy_log.close()


def stop(greffon_info):
    return subprocess.Popen(
        ['docker-compose', '-p', greffon_info['id'], '-f',
         os.path.join(get_greffon_path(greffon_info), 'docker-compose.yml'),
         'stop'],
        env=_compose_env())


# Bound the WAITED down -- a hung docker daemon must not pin the per-instance
# lock forever (the caller holds it for the whole decommission).
_DOWN_TIMEOUT_SECONDS = 300


def down(instance_id):
    """Permanently tear an instance's containers + networks + NAMED volumes down.

    WAITED (``subprocess.run``, unlike the fire-and-forget ``Popen`` of
    start/stop) so the caller can verify removal. Idempotent: a missing compose
    file is a no-op (the instance was never started, or is already gone -- the
    caller's volume prune is the authoritative cleanup), and ``down`` on an
    already-removed project exits 0. Builds the path INLINE rather than via
    ``get_greffon_path``, which would recreate the very directory we are tearing
    down. Returns the CompletedProcess (or None when skipped)."""
    path = os.path.join(os.getenv('GREFFON_PATH', '/data'), instance_id)
    compose_file = os.path.join(path, 'docker-compose.yml')
    if not os.path.exists(compose_file):
        return None
    return subprocess.run(
        ['docker-compose', '-p', instance_id, '-f', compose_file,
         'down', '-v', '--remove-orphans'],
        env=_compose_env(), capture_output=True, text=True,
        timeout=_DOWN_TIMEOUT_SECONDS)


# Label a catalog service carries to declare it a one-shot lifecycle helper
# (DB migration, object-store bucket creation, first-run superuser seed).
# Such a container runs to completion and then sits in ``exited`` forever,
# which is normal. It must NOT drag the instance into a mixed ``unknow``
# status. The status computation excludes any container carrying this label.
STATUS_IGNORE_LABEL = 'com.greffon.status'
STATUS_IGNORE_VALUE = 'ignore'


def _ignore_for_status(container):
    """Whether a container is excluded from the instance-status computation.

    Two mechanisms, both meaning "this container's state never reflects
    instance health":

    1. The ``com.greffon.status: ignore`` label: the general, per-container
       declaration the catalog author puts on a one-shot service. Covers any
       one-shot regardless of name or restart policy (e.g. the Docs/Visio
       ``createbuckets`` helper uses ``restart: on-failure``, not ``"no"``,
       so it can't be inferred from the restart policy).
    2. Legacy fallback: a name containing ``migrate``. Predates the label and
       is kept so instances started from an *unlabelled* compose (older
       catalog) don't regress when a greffer is upgraded ahead of the catalog.
       Removable once every template carries the label.

    The exclusion is unconditional. It does not look at the exit code,
    matching the legacy ``migrate`` skip. The catalog one-shots force
    ``exit 0`` regardless, and a one-shot that genuinely failed is already
    surfaced by its dependent app container failing to reach ``running``, so
    distinguishing a clean from a failed completion would add complexity for
    no signal the instance status doesn't already carry.
    """
    if 'migrate' in container.name:
        return True
    labels = container.labels or {}
    return labels.get(STATUS_IGNORE_LABEL) == STATUS_IGNORE_VALUE


def get_status(greffon_id):
    containers = []
    is_all_stopped = True
    is_all_running = True
    for container in client.containers.list(all=True, filters={'name': greffon_id}):
        if _ignore_for_status(container):
            continue
        container_status = container.status
        if container_status != 'running':
            container_status = 'stopped'
            is_all_running = False
        else:
            is_all_stopped = False
        containers.append({'status': container_status})
    if is_all_running and not is_all_stopped:
        status = 'running'
    elif not is_all_running and is_all_stopped:
        status = 'stopped'
    else:
        status = 'unknow'
    return {
        'status': status,
        'containers': containers
    }