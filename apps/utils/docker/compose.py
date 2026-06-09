import yaml
import asyncio
import json
import logging
from datauri import DataURI
from jinja2 import Environment, StrictUndefined, Template
from jinja2.exceptions import TemplateError, UndefinedError
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
_FILE_RENDER_ENV = Environment(
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
    return {
        'image': 'nginx:1.20.2-alpine-perl',
        'restart': 'unless-stopped',
        'ports': [ ('{{ports[%s].port_host}}:%s' % (i, port['port_container'])) for i, port in enumerate(greffon['ports'])],
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


def _render_baked_file(text, greffon_info, dest_name):
    """Strict-render baked file/json text; raise ConfigRenderError on a
    missing/typo'd variable (no silent empty secret)."""
    try:
        return _FILE_RENDER_ENV.from_string(text).render(**greffon_info)
    except (UndefinedError, TemplateError) as exc:
        # Log the offending variable name (UndefinedError message), never the
        # resolved secret values.
        logger.error("baked-file render failed for %s: %s", dest_name, exc)
        raise ConfigRenderError(f"{dest_name}: {exc}") from exc


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
    first_port = ports[0] if ports and isinstance(ports[0], dict) else {}
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
    return greffon_info


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
                text = json.dumps(configuration['value'])
                if destination.get('x-greffon-render'):
                    text = _render_baked_file(text, greffon_info, destination['name'])
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
                    text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
                    data = _render_baked_file(text, greffon_info, destination['name']).encode('utf-8')
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


def start(greffon_info):
    return subprocess.Popen(['docker-compose', '-f', os.path.join(get_greffon_path(greffon_info), 
    'docker-compose.yml'), 'up'])

def stop(greffon_info):
    return subprocess.Popen(['docker-compose', '-f', os.path.join(get_greffon_path(greffon_info), 
    'docker-compose.yml'), 'stop'])


def get_status(greffon_id): 
    containers = []
    is_all_stopped = True
    is_all_running = True
    #Todo should find a way to have all status pullling error...
    for container in client.containers.list(all=True, filters={'name': greffon_id}):
        if 'migrate' not in container.name:
            container_status = container.status
            if container_status != 'running': 
                container_status = 'stopped'
                is_all_running = False
            else:
                is_all_stopped = False
            containers.append({
                'status': container_status
            })
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