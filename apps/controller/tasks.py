from django_q.tasks import schedule, Schedule
from apps.utils.greffon.base_server import register
schedule('apps.utils.greffon.base_server.register',
         schedule_type=Schedule.ONCE)