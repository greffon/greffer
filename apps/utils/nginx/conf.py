import os
from jinja2 import Template

def create_nginx_conf(greffon_info):
    greffon_path = os.path.join(os.getenv('GREFFON_PATH', '/data'), greffon_info['id'])
    file = open(os.path.join(os.path.dirname(__file__),'template', 'nginx.conf.jinja'))
    t = Template(file.read())
    # nginx only fronts L7 (Tier-A) ports. L4 (Tier-C) ports are published
    # directly on their service and must get NO upstream/server block here, or
    # nginx would try to TLS-terminate a raw TCP/UDP port. Pre-filter so the
    # template stays dumb (it iterates http_ports, not ports).
    greffon_info['http_ports'] = [
        port for port in greffon_info.get('ports', [])
        if port.get('exposure_tier', 'http') != 'l4'
    ]
    compose_file = t.render(**greffon_info)
    with open(os.path.join(greffon_path, 'nginx.conf'), 'w') as temp_file:
        temp_file.write(compose_file)