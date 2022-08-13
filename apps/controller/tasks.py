from django_q.tasks import async_task
from apps.utils.greffon.base_server import register
from apps.utils.greffon import monitoring
async_task(register)

def async_monitoring(greffon_id):
    return async_task(monitoring.monitor_status, greffon_id)