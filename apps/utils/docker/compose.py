import yaml
import asyncio
import json
from datauri import DataURI
from jinja2 import Template
import docker
import subprocess
import os

client = docker.from_env()

from apps.utils.docker.volume import docker_copy_file_into_volume, docker_create_volume, docker_is_volume_exist

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
    """Expose instance_url / instance_host / instance_port / instance_id to
    the Jinja render context so catalog metadata default_value strings can
    reference `{{ instance_url }}`, `{{ instance_host }}`, etc. The greffer's
    public hostname is read from GREFFER_PUBLIC_HOST (set by the greffer's
    compose) with a dev-friendly `host.docker.internal` fallback."""
    host = os.getenv('GREFFER_PUBLIC_HOST', 'host.docker.internal')
    ports = greffon_info.get('ports') or []
    port = ports[0].get('port_host') if ports and isinstance(ports[0], dict) else ''
    scheme = os.getenv('GREFFER_PUBLIC_SCHEME', 'https')
    greffon_info.setdefault('instance_id', greffon_info.get('id', ''))
    greffon_info.setdefault('instance_host', host)
    greffon_info.setdefault('instance_port', port)
    greffon_info.setdefault(
        'instance_url',
        f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}",
    )
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
                with open(file_path, "w") as f:   # Opens file and casts as f 
                    f.write(json.dumps(configuration['value']))
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
                uri = DataURI(configuration['value']['file'])
                with open(file_path, "wb") as f:   # Opens file and casts as f 
                    f.write(uri.data)
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