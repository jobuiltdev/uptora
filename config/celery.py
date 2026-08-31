"""Celery application.

One periodic entry only -- the dispatcher. Per-monitor schedules live in the
database, never in the broker, so nothing has to be reconciled between the two
when a monitor is created, disabled or retimed.
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('uptora')

# Every CELERY_* setting in Django settings becomes a Celery setting here.
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
