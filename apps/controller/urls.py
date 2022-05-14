from django.urls import path
from django.contrib.auth import views as auth_views
from apps.controller import views


urlpatterns = [
    path('start/', views.start_greffon),
    path('stop/', views.stop_greffon),
    path('greffon/<uuid:id>/', views.greffon_status),
]
