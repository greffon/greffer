#!/usr/bin/env python
# -*- coding: utf-8 -*-

from django.urls import path
from django.urls import include, path
urlpatterns = [
    path('controller/', include('apps.controller.urls')),
]
