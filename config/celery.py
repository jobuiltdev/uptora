"""Celery application.

Two global periodic dispatchers are configured: one for due monitors and one
notification safety net. Per-resource schedules live in the database, never
as one Beat entry per monitor or delivery.
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('uptora')

# Every CELERY_* setting in Django settings becomes a Celery setting here.
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
