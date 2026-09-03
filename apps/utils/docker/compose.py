import yaml
import asyncio
import time
import copy
import re
import json
import logging
from datauri import DataURI
from jinja2 import ChainableUndefined, Environment, StrictUndefined, Template, nodes
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
# `{}`, `|default(..)` and `.get(k, d)` give the default, and
# `{% if oidc %}` takes the else branch. Same semantics `smtp` has
# shipped with. So the loud-refusal guarantee covers a DEREFERENCE, not
# every reference -- a realm file written with `|default` renders empty
# rather than refusing. A Keycloak-style realm file referencing
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


# The undo below costs one dump+compile+render per attempt. It tries
# keys in batches of _UNDO_CHUNK and only splits a batch that fails, so
# a clean run needs ceil(n / _UNDO_CHUNK) attempts.
#
# The bound is WALL CLOCK, not a render count. What a render costs is
# catalog-controlled and unbounded -- `{{ range(20000000)|sum }}` or a
# large string multiply take seconds each -- so capping the count
# capped the wrong thing: 200 renders of a slow document is still
# minutes of CPU on `/start/`. A time budget bounds the actual damage
# whatever a single render costs. Real catalog entries never reach here
# at all; when they do, the whole undo is milliseconds.
_UNDO_CHUNK = 8
_UNDO_TIME_BUDGET = 5.0


def _document_renders(compose, render_context):
    """Does the compose survive the WHOLE render once dumped?

    Dump, compile, render -- the three steps `create_compose` performs,
    with the same context, so the answer is the one that matters rather
    than an approximation of it.

    Compiling alone was not enough, twice over. `Template()` compiles as
    well as parses, and a family of `TemplateAssertionError`s comes only
    from the compile step. And a symbol can be DEFINED in one env value
    and USED in another: popping the definition (it dereferences an
    unset type) while keeping the use (it dereferences nothing) leaves a
    document that compiles perfectly and raises `'u' is undefined` at
    render:

        A: '{% macro u(p) %}{{ oidc.issuer }}{{ p }}{% endmacro %}'
        B: '{{ u(1) }}'

    Any failure is a no, including a compose that will not dump.
    """
    try:
        Environment().from_string(yaml.dump(compose)).render(**render_context)
    except Exception:
        return False
    return True


def _env_items_by_service(compose):
    """`{service: [item]}` where an item is what the strip can remove.

    Mapping form gives `(key, value)` pairs; list form gives the raw
    `"KEY=value"` entries. ITEMS, not key names: a list-form
    `environment` may legally carry the same name twice, and popping one
    occurrence must not read as "nothing was popped" -- that made a
    required pop invisible to the undo, which then silently restored
    glitchtip's `EMAIL_URL` to render `smtp://:@`. Keying on the whole
    item also stops the undo removing the OTHER occurrence, which
    matching by name would.

    Lists, not sets: the order these come out in decides which pops are
    replayed first, and a set of strings iterates differently between
    processes. Keeping the document's own order makes the undo
    reproducible without sorting anything.
    """
    services = compose.get('services') if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        return {}
    found = {}
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        env = service.get('environment')
        if isinstance(env, dict):
            found[name] = [(k, v) for k, v in env.items()]
        elif isinstance(env, list):
            found[name] = list(env)
    return found


def _items_removed(before, after):
    """Multiset difference, order-preserving.

    `list.remove` compares with `==`, so this works for the unhashable
    values YAML can produce (a list- or dict-valued env var), which a
    `Counter` would refuse.
    """
    remaining = list(after)
    removed = []
    for item in before:
        if item in remaining:
            remaining.remove(item)
        else:
            removed.append(item)
    return removed


def _remove_env_item(compose, service_name, item):
    """Remove ONE occurrence of `item` from one service's env block."""
    service = compose.get('services', {}).get(service_name)
    if not isinstance(service, dict):
        return
    env = service.get('environment')
    if isinstance(env, dict):
        key, value = item
        if key in env and env[key] == value:
            del env[key]
    elif isinstance(env, list) and item in env:
        env.remove(item)


def _delete_unset_integration_env_keys(compose, greffon_info):
    """For each known integration type whose config is unset, pop every
    env key in the compose that would expand to an unset-integration
    Jinja reference. This guarantees ``absent ⇒ no env var`` regardless
    of how Jinja renders ``{{ smtp.host }}`` on an empty dict — and,
    crucially, regardless of whether the catalog destination metadata
    actually reached the greffer for this start.

    Two passes:

    1. **Metadata-driven**: walk ``greffon_info['configurations']``
       (the manager-sent destination list) and pop keys whose
       ``destination.type`` matches an unset integration. Works when
       the manager sent the full catalog destination set.

    2. **Template-driven** (new — fixes Nextcloud install on no-SMTP):
       walk every service's environment and pop any entry whose value
       contains ``{{ <unset-type>.* }}``. Works even when the manager
       only sent user-submitted configurations (the historical shape):
       Nextcloud's ``MAIL_FROM_ADDRESS: '{{ smtp.from_address.split(...) }}'``
       has no per-instance value (user didn't pick SMTP, so the manager
       has nothing to send) so the metadata pass alone can't see it;
       the template pass catches it directly from the compose body
       before Jinja renders.

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
    # question is the render's question and not a proxy for it -- but a
    # DEEP COPY of it. `_compose_render_context` copies only the top
    # level, and the guard now RENDERS, so a side-effecting expression
    # would run against the caller's own objects and be observed by
    # everything downstream: `{{ volumes.popitem() and '' }}` emptied
    # `greffon_info['volumes']`, which `create_volumes_then_copy_files`
    # consumes two lines after `create_compose` returns. The catalog is
    # treated as hostile elsewhere in this module, so it is treated as
    # hostile here.
    #
    # A context that will not deep-copy means the guard cannot be run
    # safely, so it is skipped rather than run dangerously.
    try:
        render_context = copy.deepcopy(_compose_render_context(greffon_info))
    except Exception:
        render_context = None
    rendered_before = (
        render_context is not None
        and _document_renders(compose, render_context)
    )
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
                    env.pop(key, None)
                elif isinstance(env, list):
                    prefix = f'{key}='
                    service['environment'] = [
                        e for e in env if not (isinstance(e, str) and e.startswith(prefix))
                    ]

    # Pass 2 -- template-driven pop, for values the catalog templates
    # rather than declares. `{{ smtp.host }}`,
    # `{{ smtp.from_address.split('@')[0] }}`, the dict-index form
    # `{{ {"tls": "ssl"}[smtp.tls_mode] }}` and the bracket form
    # `{{ smtp['from_address'] }}` are all references for this purpose.
    #
    # ASKS JINJA'S OWN LEXER rather than approximating it.
    #
    # Five hand-rolled versions preceded this, and each one shipped a
    # different way of being wrong about Jinja syntax: a whole-string
    # match popped a literal `oidc.<host>` beside an unrelated `{{ }}`;
    # scanning only `{{ }}` missed statement blocks; a non-greedy body
    # match stopped at a `}}` inside a string literal; then, in turn,
    # spaced dots (`{{ config . oidc . url }}`), nested mapping braces
    # (`{{ {"a": {}} and smtp.port|int }}`), a call's closing paren
    # (`{{ dict(oidc).get(..) }}`), the same with a space before the
    # paren, and finally a `\s*\.\s*` normalisation that backtracked
    # quadratically on a whitespace run -- a denial of service on input
    # the catalog controls, reached through `/start/`.
    #
    # Every one of those is a place a regex approximates a parser and
    # leaks. The lexer does not approximate: string literals, comments,
    # `{% raw %}`, whitespace and nesting are its job, and it is a linear
    # scan, so the backtracking class cannot recur either.
    #
    # A reference is a NAME token for the type that (a) is not itself an
    # attribute of something else, and (b) is followed by `.` or `[`.
    # Grouping parens are seen through; a CALL's parens are not, which is
    # what separates `(oidc).issuer` from `dict(oidc).get(..)`.
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

        Still incomplete in the same two places the lexer was, and for the
        same reason -- aliasing (`{% set a = oidc %}{{ a.issuer }}`, a macro
        argument) and `|map(attribute='a.b')` move the dereference off this
        name entirely. Deciding those needs dataflow, not syntax.

        The binding covers the SIMPLE aliases -- `{{ base }}` where
        `base` was set from `oidc.url` renders empty rather than
        raising. It does NOT cover a name bound to a definition that
        gets popped: the binding knows `oidc`, not the macro named after
        it. `{% macro u() %}{{ oidc.x }}{% endmacro %}` in one env value
        and `{{ u() }}` in another raises `'u' is undefined`. That is
        what the document guard is for.

        Over-pops a locally shadowed name (`{% for oidc in xs %}`), exactly
        as the lexer and `main` both do. Over-pop costs an env var;
        under-pop costs the deploy.
        """
        try:
            ast = parser_env.parse(value)
        except Exception:
            # Deliberately bare: the body is a single `parse()` on
            # catalog-controlled input, so there is no logic of ours for
            # a broad catch to hide, and anything escaping is a 500 at
            # `/start/`. Naming the classes was tried and failed three
            # times -- TemplateError, RecursionError, ValueError and
            # finally SyntaxError (Jinja's number lexer accepts non-ASCII
            # digits, then hands `0.１` to the CPython compiler), each
            # found only after the previous fix shipped. `Exception`, not
            # `BaseException`, so a KeyboardInterrupt still stops the
            # process.
            #
            # Fall back to the crudest form of the question: does the
            # name occur at all? A value that never mentions `oidc`
            # cannot dereference it except by aliasing, the residual
            # named above. What is left -- broken and not mentioning the
            # name -- keeps `main`'s behaviour and fails the render
            # loudly, rather than being silently dropped from a deploy
            # that then comes up misconfigured.
            #
            # This deliberately does NOT also pop on `{%`. That rule
            # existed to stop a block being half-popped and left
            # dangling, but it popped values referencing no integration
            # at all (`{% if instance_url %}` split across two values
            # lost both keys on every instance with an unset type, which
            # today is every instance). The document guard below
            # supersedes it and is not restricted to shapes anyone
            # thought of.
            return re.search(rf'\b{re.escape(name)}\b', value) is not None
        for node in ast.find_all((nodes.Getattr, nodes.Getitem)):
            target = node.node
            # No `ctx` check: a store-context Getattr is unreachable from
            # Jinja source (`{% set oidc.a = 1 %}` parses to no Getattr,
            # `{% for oidc.a in xs %}` is a syntax error), so testing it
            # would pin nothing.
            if isinstance(target, nodes.Name) and target.name == name:
                return True
        return False

    def matching_unset_types(value):
        """Which unset types `value` dereferences, in tuple order."""
        if not isinstance(value, str):
            return ()
        if '{{' not in value and '{%' not in value:
            return ()
        return tuple(t for t in unset_types if _dereferences(value, t))


    def _log_pop(key, name, matched):
        # The types that MATCHED, not every unset type: on a fresh
        # instance both smtp and oidc are unset, so naming the whole set
        # would report a key that references only oidc as
        # "unset type: smtp, oidc" and make the field noise.
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

    # `snapshot is not None` already implies the document rendered
    # before and could be copied; re-testing `rendered_before` here
    # would be a second spelling of the same condition.
    if snapshot is not None and not _document_renders(compose, render_context):
        _reapply_pops_that_keep_it_renderable(
            compose, snapshot, greffon_info, render_context)
    return compose


def _reapply_pops_that_keep_it_renderable(
        compose, snapshot, greffon_info, render_context):
    """Keep every pop that is safe on its own; undo only the ones that
    break the document.

    Reverting the strip WHOLESALE was wrong, because the document is one
    template but the damage is local: a malformed value in one service
    un-popped every correctly-popped key in every OTHER service, and
    glitchtip's `EMAIL_URL` came back to render `smtp://:@`, which is the
    exact malformed URL the strip exists to prevent. "Weaker guarantee
    but still a working deploy" was not true for that app.

    So rebuild from the snapshot and re-apply the pops one at a time,
    keeping each only while the document still compiles. The pops that
    are fine survive; the one that straddles a construct is dropped.

    Cost is paid only here, in the case that already went wrong -- the
    common path is the two `_document_parses` calls above. Order is
    deterministic so the outcome does not depend on dict iteration luck.
    Applying the FULL set is never retried: it is what just failed.
    """
    before = _env_items_by_service(snapshot)
    after = _env_items_by_service(compose)
    popped = [
        (service, item)
        for service, items in before.items()
        for item in _items_removed(items, after.get(service, ()))
    ]

    # Deciding one pop at a time costs a full dump+compile+render per
    # step, which is quadratic in the number of popped keys. Chunk it:
    # try a whole batch, and only fall back to one-at-a-time for a batch
    # that fails. A real catalog entry pops 10-30 keys and almost never
    # reaches here at all; the custom-compose epic makes this input
    # user-controlled, so the work is also capped.
    #
    # Running out of budget undoes only the pops not yet DECIDED, never
    # the ones already kept. Restoring everything wholesale was the
    # earlier behaviour and it re-introduced the bug this function
    # exists to prevent: glitchtip's `EMAIL_URL` came back to render
    # `smtp://:@`. Degrading has to preserve the decisions already made.
    last_good = copy.deepcopy(snapshot)
    kept, undone = [], []
    deadline = time.monotonic() + _UNDO_TIME_BUDGET
    index = 0

    def label(service, item):
        name = item[0] if isinstance(item, tuple) else str(item).split('=', 1)[0]
        return f'{service}.{name}'

    def out_of_time():
        return time.monotonic() >= deadline

    def try_removing(batch):
        """Apply `batch` to a copy of `last_good`; keep it if it renders."""
        nonlocal last_good
        trial = copy.deepcopy(last_good)
        for service, item in batch:
            _remove_env_item(trial, service, item)
        if _document_renders(trial, render_context):
            last_good = trial
            return True
        return False

    while index < len(popped) and not out_of_time():
        chunk = popped[index:index + _UNDO_CHUNK]
        if try_removing(chunk):
            kept.extend(label(*one) for one in chunk)
            index += len(chunk)
            continue
        if len(chunk) == 1:
            undone.append(label(*chunk[0]))
            index += 1
            continue
        # The batch failed, so decide its members individually. `index`
        # only advances past ones actually decided, so running out of
        # budget mid-chunk leaves the rest to the tail below.
        for one in chunk:
            if out_of_time():
                break
            if try_removing([one]):
                kept.append(label(*one))
            else:
                undone.append(label(*one))
            index += 1

    if index < len(popped):
        # Out of time with keys still undecided. Leaving them un-popped
        # is safe for the RENDER but drops the strip's actual guarantee
        # for the tail, and the tail is chosen by nothing but document
        # order -- that is how glitchtip's `EMAIL_URL` came back to
        # render `smtp://:@`. So spend one more render trying the whole
        # remainder at once: the poison is usually a key already
        # decided, in which case everything left is fine together.
        rest = popped[index:]
        if try_removing(rest):
            kept.extend(label(*one) for one in rest)
            index = len(popped)
        else:
            undone.extend(label(*one) for one in rest)
        logger.warning(
            'integration strip on instance %s ran out of undo budget (%.1fs) '
            'with %d popped keys; the remaining %d were %s',
            greffon_info.get('id'), _UNDO_TIME_BUDGET, len(popped), len(rest),
            'all applied together' if index == len(popped)
            else 'left un-popped, so they render empty rather than absent',
        )

    logger.warning(
        'integration strip left instance %s unrenderable; kept %s and undid '
        '%s, which will render empty through the unset binding instead',
        greffon_info.get('id'),
        ', '.join(kept) or 'nothing',
        ', '.join(undone) or 'nothing',
    )
    compose.clear()
    compose.update(last_good)


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
    t = Template(yaml.dump(compose))
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