import copy
from django.test import TestCase
from apps.controller.serializer import (
    GreffonStartSerializer,
    GreffonStopSerializer,
    CerificateSerializer,
)
from tests.helpers import SAMPLE_START_PAYLOAD


class GreffonStartSerializerTests(TestCase):
    """Tests for the GreffonStartSerializer."""

    def test_start_serializer_valid(self):
        """Full payload with all fields should be valid."""
        serializer = GreffonStartSerializer(data=SAMPLE_START_PAYLOAD)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_start_serializer_missing_cert(self):
        """Payload without the required cert field should be invalid."""
        payload = copy.deepcopy(SAMPLE_START_PAYLOAD)
        del payload['cert']
        serializer = GreffonStartSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn('cert', serializer.errors)

    def test_start_serializer_missing_id(self):
        """Payload without the required id field should be invalid."""
        payload = copy.deepcopy(SAMPLE_START_PAYLOAD)
        del payload['id']
        serializer = GreffonStartSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn('id', serializer.errors)


class GreffonStopSerializerTests(TestCase):
    """Tests for the GreffonStopSerializer."""

    def test_stop_serializer_valid(self):
        """Payload with id should be valid."""
        serializer = GreffonStopSerializer(data={'id': 'abc'})
        self.assertTrue(serializer.is_valid(), serializer.errors)


class CerificateSerializerTests(TestCase):
    """Tests for the CerificateSerializer."""

    def test_certificate_serializer_valid(self):
        """Payload with certificate and private_key should be valid."""
        serializer = CerificateSerializer(data={
            'certificate': 'CERT',
            'private_key': 'KEY',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
