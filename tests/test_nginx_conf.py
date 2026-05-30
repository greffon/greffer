import os
import tempfile
import unittest
from unittest.mock import patch, mock_open


class CreateNginxConfTests(unittest.TestCase):
    """Tests for create_nginx_conf."""

    def test_create_nginx_conf_writes_file(self):
        """create_nginx_conf() should render the Jinja2 template and write the
        result to nginx.conf inside the greffon path."""
        from apps.utils.nginx.conf import create_nginx_conf

        template_content = 'server { listen {{ port }}; server_name {{ name }}; }'
        greffon_info = {
            'id': 'test-greffon-1',
            'port': '8080',
            'name': 'myapp',
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_path = os.path.join(tmpdir, greffon_info['id'])
            os.makedirs(greffon_path)

            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                # Mock the template file read (the open call for the .jinja file)
                # but let the output file write use the real filesystem
                original_open = open

                def side_effect_open(path, *args, **kwargs):
                    if path.endswith('nginx.conf.jinja'):
                        return mock_open(read_data=template_content)()
                    return original_open(path, *args, **kwargs)

                with patch('builtins.open', side_effect=side_effect_open):
                    create_nginx_conf(greffon_info)

            # Verify the output file was written
            output_path = os.path.join(greffon_path, 'nginx.conf')
            self.assertTrue(os.path.exists(output_path))

            with open(output_path) as f:
                content = f.read()
            self.assertIn('8080', content)
            self.assertIn('myapp', content)

    def test_real_template_forwards_proto_https(self):
        """REGRESSION: the rendered nginx.conf MUST set
        ``X-Forwarded-Proto: https`` on every proxied request.

        Without this header, apps that 301-upgrade based on the request
        protocol (Ghost is the canonical example — Discourse, GitLab,
        Mastodon, Vaultwarden, anything Express/Rails with auto-HTTPS
        redirect) see the plain-HTTP request from greffon_nginx, see
        their configured https:// public URL, and 301-loop indefinitely.

        Hardcoded ``https`` is correct here because the only listener in
        the template is ``listen <port> ssl``, so any request reaching
        a proxied location came in via TLS.

        Test renders the *real* template (not a mock) so the assertion
        guards the file on disk, not the Jinja machinery.
        """
        from apps.utils.nginx.conf import create_nginx_conf

        greffon_info = {
            'id': 'xfp-regression',
            'ports': [
                {'container_name': 'app', 'port_container': '8080'},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_path = os.path.join(tmpdir, greffon_info['id'])
            os.makedirs(greffon_path)
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                create_nginx_conf(greffon_info)
            with open(os.path.join(greffon_path, 'nginx.conf')) as f:
                content = f.read()

        self.assertIn(
            'proxy_set_header X-Forwarded-Proto https;',
            content,
            'X-Forwarded-Proto must be set so canonical-https apps don\'t loop',
        )
        # Sanity: the location block must still be there. If the template
        # ever drops the proxy block the assertion above passes by
        # absence of the substring; this catches that.
        self.assertIn('location /', content)
        self.assertIn('proxy_pass http://app_8080/', content)

    def test_create_nginx_conf_renders_template(self):
        """Jinja2 variables from greffon_info should be substituted in the
        rendered output."""
        from apps.utils.nginx.conf import create_nginx_conf

        template_content = (
            'upstream {{ app_name }} { server {{ host }}:{{ port }}; }\n'
            'server { listen {{ port }}; }'
        )
        greffon_info = {
            'id': 'render-test',
            'app_name': 'wordpress',
            'host': 'web-container',
            'port': '443',
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_path = os.path.join(tmpdir, greffon_info['id'])
            os.makedirs(greffon_path)

            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                original_open = open

                def side_effect_open(path, *args, **kwargs):
                    if path.endswith('nginx.conf.jinja'):
                        return mock_open(read_data=template_content)()
                    return original_open(path, *args, **kwargs)

                with patch('builtins.open', side_effect=side_effect_open):
                    create_nginx_conf(greffon_info)

            output_path = os.path.join(greffon_path, 'nginx.conf')
            with open(output_path) as f:
                content = f.read()

            # Verify all Jinja2 variables were substituted
            self.assertIn('wordpress', content)
            self.assertIn('web-container', content)
            self.assertIn('443', content)
            # Verify no unrendered Jinja2 placeholders remain
            self.assertNotIn('{{', content)
            self.assertNotIn('}}', content)
