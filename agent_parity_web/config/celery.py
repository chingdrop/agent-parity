"""Celery application, bound to Django settings.

All Celery configuration lives in Django settings under the ``CELERY_``
namespace (broker URL, result backend, eager mode for tests, beat schedule)
so there is exactly one configuration surface and no prefix collisions with
unrelated Django settings.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("agent_parity")
app.config_from_object("django.conf:settings", namespace="CELERY")

# Pick up dashboard/tasks.py (and any future app's tasks module) automatically.
app.autodiscover_tasks()
