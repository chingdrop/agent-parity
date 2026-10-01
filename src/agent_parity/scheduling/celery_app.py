"""Celery application: scheduled fan-out/fan-in on top of ``agent_parity.scheduling.tasks``.

Broker and result backend come straight from the environment and both
default to a local Redis instance (``docker/docker-compose.yml`` runs one).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from celery import Celery
from celery.schedules import crontab

app = Celery("agent_parity", include=["agent_parity.scheduling.tasks"])

app.conf.broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
app.conf.result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

# Beat schedule: hourly tick lets each client's own ClientConfig.sync_interval_hours
# decide whether it's actually due (see agent_parity.scheduling.tasks.dispatch_all_clients);
# the daily 07:00 forced run guarantees at least one full sync a day even for a
# client whose cadence would otherwise line up to skip that slot.
app.conf.beat_schedule = {
    "dispatch-due-clients-hourly": {
        "task": "agent_parity.scheduling.tasks.dispatch_all_clients",
        "schedule": 3600.0,
    },
    # Just before the forced 07:00 run, so it classifies against current dates.
    "refresh-os-eol-data-daily": {
        "task": "agent_parity.scheduling.tasks.refresh_os_eol_data",
        "schedule": crontab(hour=6, minute=30),
    },
    "dispatch-all-clients-daily-7am": {
        "task": "agent_parity.scheduling.tasks.dispatch_all_clients",
        "schedule": crontab(hour=7, minute=0),
        "kwargs": {"force": True},
    },
}


@contextmanager
def run_eagerly() -> Iterator[None]:
    """Run every task in-process for the duration of the block.

    ``agent-parity run`` without ``--workers`` uses this to execute the same
    chord beat dispatches, with no broker or worker: the fan-out tasks run
    one after another in this process instead of in parallel on workers.
    Exceptions are captured in the task result rather than raised at
    dispatch, as on a real worker — that's what lets the chord's
    ``link_error`` backstop mark a failed run FAILED instead of leaving it
    PENDING. Restores the previous settings on exit.
    """
    saved = app.conf.task_always_eager, app.conf.task_eager_propagates
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = False
    try:
        yield
    finally:
        app.conf.task_always_eager, app.conf.task_eager_propagates = saved
