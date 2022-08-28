from rest_framework import serializers

class GreffonFieldSerializer(serializers.Serializer):
    value = serializers.JSONField()
    destinations = serializers.JSONField()

class CerificateSerializer(serializers.Serializer):
    certificate = serializers.CharField(label='certificate')
    private_key = serializers.CharField(label='private_key')

class GreffonStartSerializer(serializers.Serializer):
    id = serializers.CharField(label='ID')
    repository_url = serializers.CharField(label='Repository Url')
    cert = CerificateSerializer(required=True)
    configurations = GreffonFieldSerializer(many=True, required=False)
    ports = serializers.DictField(required=False)

class GreffonStopSerializer(serializers.Serializer):
    id = serializers.CharField(label='ID')