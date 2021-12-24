from rest_framework import serializers

class GreffonFieldSerializer(serializers.Serializer):
    value = serializers.JSONField()
    destinations = serializers.JSONField()

class GreffonStartSerializer(serializers.Serializer):
    id = serializers.CharField(label='ID')
    repository_url = serializers.CharField(label='Repository Url')
    configurations = GreffonFieldSerializer(many=True)

class GreffonStopSerializer(serializers.Serializer):
    id = serializers.CharField(label='ID')