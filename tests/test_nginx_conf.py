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
