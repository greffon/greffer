import json
import threading
from apps.controller.serializer import GreffonStartSerializer, GreffonStopSerializer
from apps.utils.docker import compose
from apps.utils.greffon import repository
from apps.utils.nginx import conf
from django.http import JsonResponse
from apps.utils.greffon.base_server import register
from rest_framework.decorators import api_view
from apps.utils.auth import is_logged
from apps.utils.greffon.monitoring import monitor_status

def async_task(task_func, *args, **kwargs):
    task = threading.Thread(target=task_func, daemon=True, args=args, kwargs=kwargs)
    task.start()
    return task
async_task(register)
async_task(monitor_status)
@api_view(['POST'])
@is_logged
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
@is_logged
def stop_greffon(request):
    greffon_form = GreffonStopSerializer(data=json.loads(request.body))
    if not greffon_form.is_valid():
        return JsonResponse({
        'message': 'Invalid Fields', 
        'errors': dict(greffon_form.errors.items())}, 
                        status=400)
    compose.stop(greffon_form.data)
    return JsonResponse({}, status=200) 


@api_view(['GET'])
@is_logged
def greffon_status(_, id):
    return JsonResponse(compose.status(id), status=200) 
