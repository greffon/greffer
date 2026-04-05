SAMPLE_CERT = {
    'certificate': '-----BEGIN CERTIFICATE-----\nFAKECERT\n-----END CERTIFICATE-----',
    'private_key': '-----BEGIN RSA PRIVATE KEY-----\nFAKEKEY\n-----END RSA PRIVATE KEY-----',
}

SAMPLE_COMPOSE = {
    'version': '3',
    'services': {
        'app': {
            'image': 'wordpress:latest',
            'ports': ['8080:80'],
            'volumes': ['app_data:/var/www/html'],
        }
    },
    'volumes': {'app_data': {}},
}

SAMPLE_START_PAYLOAD = {
    'id': 'test-instance-123',
    'repository_url': 'https://example.com/docker-compose.yml',
    'cert': SAMPLE_CERT,
    'configurations': [
        {
            'value': {'db_host': 'localhost'},
            'destinations': [
                {'type': 'json', 'name': 'config.json', 'volume': 'app_data'}
            ]
        }
    ],
    'ports': {
        'app_80': {'url': 'https://field-uuid.my.greffon.io'}
    }
}
