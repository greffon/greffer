import json
from apps.controller.serializer import GreffonStartSerializer, GreffonStopSerializer
from apps.utils.docker import compose
from apps.utils.greffon import repository
from apps.utils.nginx import conf
from django.http import JsonResponse
from apps.utils.greffon.base_server import register
from rest_framework.decorators import api_view
from apps.utils.auth import is_logged


register()
@api_view(['POST'])
def start_greffon(request):
    greffon_form = GreffonStartSerializer(data=json.loads(request.body))
    if not greffon_form.is_valid():
        return JsonResponse({
        'message': 'Invalid Fields', 
        'errors': dict(greffon_form.errors.items())},
                        status=400)
    greffon_form = greffon_form.data
    compose_file = repository.get_compose_file_from_repository(greffon_form)
    greffon_info = repository.get_greffon_info(compose_file, greffon_form)
    compose_template = compose.get_compose_template(compose_file, greffon_info)
    compose.apply_configuration(greffon_info, compose_file)
    compose.create_compose(compose_template, greffon_info)
    conf.create_nginx_conf(greffon_info)
    compose.create_volumes_then_copy_files(greffon_info)
    compose.start(greffon_info)
    return JsonResponse({
        'ports': greffon_info['ports']}, 
                        status=200)
@api_view(['POST'])
def stop_greffon(request):
    greffon_form = GreffonStopSerializer(data=json.loads(request.body))
    if not greffon_form.is_valid():
        return JsonResponse({
        'message': 'Invalid Fields', 
        'errors': dict(greffon_form.errors.items())}, 
                        status=400)
    compose.stop(greffon_form.data)
    return JsonResponse({}, status=200) 
