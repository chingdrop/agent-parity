"""Celery application: scheduled fan-out/fan-in on top of ``agent_parity.tasks``.

Historically this lived in a separate Django project bound to Django
settings (``config_from_object("django.conf:settings", namespace="CELERY")``)
— there's no Django here, so broker/backend come straight from the
environment. Broker and result backend both default to a local Redis
instance (``docker/docker-compose.yml`` runs one), same default the
historical Docker Compose stack used.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

app = Celery("agent_parity", include=["agent_parity.tasks"])

app.conf.broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
app.conf.result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

# Beat schedule: hourly tick lets each client's own ClientConfig.sync_interval_hours
# decide whether it's actually due (see agent_parity.tasks.dispatch_all_clients);
# the daily 07:00 forced run guarantees at least one full sync a day even for a
# client whose cadence would otherwise line up to skip that slot.
app.conf.beat_schedule = {
    "dispatch-due-clients-hourly": {
        "task": "agent_parity.tasks.dispatch_all_clients",
        "schedule": 3600.0,
    },
    "dispatch-all-clients-daily-7am": {
        "task": "agent_parity.tasks.dispatch_all_clients",
        "schedule": crontab(hour=7, minute=0),
        "kwargs": {"force": True},
    },
}
