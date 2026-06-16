import yaml
import asyncio
import json
import logging
from datauri import DataURI
from jinja2 import StrictUndefined, Template
from jinja2.exceptions import SecurityError, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment
import docker
import subprocess
import os
from urllib.parse import urlparse
client = docker.from_env()

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
# `destination.type: <type>` in metadata.json. V1 ships SMTP only; new
# types slot in additively here AND in the manager (per-type FK on
# GreffonInstance) AND in the catalog validator.
KNOWN_INTEGRATION_TYPES = ('smtp',)


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
    with AttributeError on None. The companion delete-on-unset pass
    strips those keys from the compose entirely; the empty-dict default
    is purely belt-and-braces in case a future delete-pass bug leaves
    a stray `{{ smtp.* }}` in place.
    """
    integrations = greffon_info.get('integrations') or {}
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

    Defensive on shape: catalog metadata is supposed to use mapping-
    form ``environment:`` per the Feature #2 validator, but compose
    YAML also permits list form (``KEY=value``); both passes handle
    each form.
    """
    integrations = greffon_info.get('integrations') or {}
    services = compose.get('services', {}) if isinstance(compose, dict) else {}

    unset_types = [
        t for t in KNOWN_INTEGRATION_TYPES
        if not _is_integration_set(integrations.get(t))
    ]
    if not unset_types:
        return compose

    # Pass 1 — metadata-driven pop (unchanged behavior).
    for t in unset_types:
        for configuration in greffon_info.get('configurations', []) or []:
            for destination in configuration.get('destinations', []) or []:
                if not isinstance(destination, dict):
                    continue
                if destination.get('type') != t:
                    continue
                container = destination.get('container')
                key = destination.get('key')
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

    # Pass 2 — template-driven pop. We want to pop any env value that
    # would expand to a reference of an unset integration type, e.g.
    # ``{{ smtp.host }}``, ``{{ smtp.from_address.split('@')[0] }}``,
    # the dict-index form
    # ``{{ {"tls": "ssl", "starttls": "tls", "none": ""}[smtp.tls_mode] }}``,
    # AND the bracket-key form ``{{ smtp['from_address'] }}`` (valid
    # Jinja, semantically identical to ``smtp.from_address`` for our
    # purposes — Codex P2 on PR #35).
    #
    # Cheap + robust: require both ``{{`` and a word-bounded ``<type>``
    # immediately followed by ``.`` (attr) or ``[`` (bracket) in the
    # value. ``\b`` before the type prevents ``foo_smtp.bar`` false
    # positives.
    import re
    type_patterns = {
        t: re.compile(r'\b' + re.escape(t) + r'(?:\.\w|\[)')
        for t in unset_types
    }

    def value_references_unset(value):
        if not isinstance(value, str):
            return False
        if '{{' not in value:
            return False
        return any(p.search(value) for p in type_patterns.values())

    for service in services.values():
        if not isinstance(service, dict):
            continue
        env = service.get('environment')
        if isinstance(env, dict):
            for key in [k for k, v in env.items() if value_references_unset(v)]:
                env.pop(key, None)
        elif isinstance(env, list):
            service['environment'] = [
                e for e in env
                if not (isinstance(e, str) and '=' in e and value_references_unset(e.split('=', 1)[1]))
            ]
    return compose


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
    _delete_unset_integration_env_keys(compose, greffon_info)
    _inject_instance_log_rotation(compose)
    t = Template(yaml.dump(compose))
    compose_file = t.render(**greffon_info)
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


def _compose_env():
    """Minimal env for the docker-compose child: structurally prevents
    ``${GREFFER_TOKEN}`` (or any other greffer-only secret) from being
    interpolated by a hostile catalog compose."""
    return {k: v for k, v in os.environ.items() if k in _COMPOSE_ENV_ALLOWLIST}


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