import os
from jinja2 import Template

def create_nginx_conf(greffon_info):
    greffon_path = os.path.join(os.getenv('GREFFON_PATH', '/data'), greffon_info['id'])
    file = open(os.path.join(os.path.dirname(__file__),'template', 'nginx.conf.jinja'))
    t = Template(file.read())
    compose_file = t.render(**greffon_info)
    with open(os.path.join(greffon_path, 'nginx.conf'), 'w') as temp_file:
        temp_file.write(compose_file)