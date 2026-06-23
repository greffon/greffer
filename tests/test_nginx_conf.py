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

    def test_real_template_streaming_label_disables_buffering(self):
        """A port flagged ``streaming`` (from the com.greffon.proxy.streaming
        label) MUST render the buffering-off directives, so SSE / long-poll
        responses are not held by nginx's default response buffering."""
        from apps.utils.nginx.conf import create_nginx_conf

        greffon_info = {
            'id': 'streaming-on',
            'ports': [
                {'container_name': 'studio', 'port_container': '3000',
                 'streaming': True},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            greffon_path = os.path.join(tmpdir, greffon_info['id'])
            os.makedirs(greffon_path)
            with patch.dict(os.environ, {'GREFFON_PATH': tmpdir}):
                create_nginx_conf(greffon_info)
            with open(os.path.join(greffon_path, 'nginx.conf')) as f:
                content = f.read()

        self.assertIn('proxy_buffering off;', content)
        self.assertIn('add_header X-Accel-Buffering no always;', content)
        self.assertIn('proxy_read_timeout 3600s;', content)

    def test_real_template_no_streaming_label_keeps_buffering(self):
        """REGRESSION: a port WITHOUT the streaming flag must NOT emit any
        buffering-off directive. Default buffering protects every normal
        request/response greffon; the streaming opt-in must stay opt-in."""
        from apps.utils.nginx.conf import create_nginx_conf

        greffon_info = {
            'id': 'streaming-off',
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

        self.assertNotIn('proxy_buffering off;', content)
        self.assertNotIn('X-Accel-Buffering', content)
        # The location block is otherwise intact.
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


class ServiceStreamingLabelTests(unittest.TestCase):
    """Tests for _service_streaming label parsing (com.greffon.proxy.streaming)."""

    def _streaming(self, service):
        from apps.utils.greffon.repository import _service_streaming
        return _service_streaming(service)

    def test_mapping_true_variants(self):
        # Values as yaml.safe_load would yield them: quoted "true" -> 'true',
        # bare true/yes/on -> bool True, 1 -> int 1, plus odd casing/spacing.
        for val in ['true', True, '1', 1, ' TRUE ']:
            self.assertTrue(
                self._streaming({'labels': {'com.greffon.proxy.streaming': val}}),
                f'{val!r} should read as streaming',
            )

    def test_mapping_falsey_or_absent(self):
        self.assertFalse(self._streaming({'labels': {'com.greffon.proxy.streaming': 'false'}}))
        self.assertFalse(self._streaming({'labels': {'other': 'true'}}))
        self.assertFalse(self._streaming({'labels': {}}))
        self.assertFalse(self._streaming({}))

    def test_list_form(self):
        self.assertTrue(self._streaming(
            {'labels': ['com.greffon.proxy.streaming=true', 'foo=bar']}))
        self.assertFalse(self._streaming(
            {'labels': ['com.greffon.proxy.streaming=false']}))
        # bare flag (no '=') is not truthy
        self.assertFalse(self._streaming({'labels': ['com.greffon.proxy.streaming']}))

    def test_malformed_labels_do_not_crash(self):
        # A bare-string or otherwise malformed labels block must read as
        # "no label", never raise and abort the start flow.
        self.assertFalse(self._streaming({'labels': 'com.greffon.proxy.streaming=true'}))
        self.assertFalse(self._streaming({'labels': [123, None]}))
        self.assertFalse(self._streaming({'labels': None}))
