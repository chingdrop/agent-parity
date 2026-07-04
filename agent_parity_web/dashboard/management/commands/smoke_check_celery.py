"""Dispatch a real Celery chord and wait for it to complete.

This proves the worker/broker/result-backend actually work end to end —
``tests/test_tasks.py`` runs the same tasks with ``task_always_eager``, which
never touches a real broker, never involves a second process picking work
up, and can't catch a misconfigured ``CELERY_BROKER_URL`` or a worker that
isn't actually running. Only meaningful inside the Docker Compose stack,
where a real Redis and a real worker container exist; used by
``docker/smoke_test.sh``, not part of the demo or production flow.
"""

import time

from django.core.management.base import BaseCommand, CommandError

from dashboard.models import CorrelationRun
from dashboard.tasks import dispatch_all_clients

TERMINAL_STATUSES = (CorrelationRun.RunStatus.COMPLETE, CorrelationRun.RunStatus.PARTIAL)


class Command(BaseCommand):
    help = "Smoke test only: dispatch a real Celery chord and wait for it to complete."

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout", type=int, default=60, help="Seconds to wait for the chord to complete."
        )

    def handle(self, *args, **options):
        timeout = options["timeout"]
        baseline = CorrelationRun.objects.filter(status__in=TERMINAL_STATUSES).count()

        # .delay() + .get() round-trips dispatch_all_clients itself through
        # the real broker/worker — a plain function call would prove nothing.
        result = dispatch_all_clients.delay(force=True)
        dispatched = result.get(timeout=timeout)
        if not dispatched:
            raise CommandError("dispatch_all_clients ran but dispatched no clients")
        self.stdout.write(f"dispatch_all_clients ran via the real worker; dispatched: {dispatched}")

        # dispatch_all_clients returning only proves *it* ran — the group+chord
        # it registered via transaction.on_commit fires asynchronously, so the
        # actual fan-out/fan-in has to be polled for separately.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            count = CorrelationRun.objects.filter(status__in=TERMINAL_STATUSES).count()
            if count > baseline:
                self.stdout.write(self.style.SUCCESS(
                    f"chord completed: {count - baseline} new run(s) reached a terminal "
                    f"status via the real broker/worker"
                ))
                return
            time.sleep(2)

        raise CommandError(
            f"no CorrelationRun reached a terminal status within {timeout}s of dispatch — "
            f"the chord fan-out/fan-in didn't complete via the real worker"
        )
