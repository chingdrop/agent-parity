"""Run the full pipeline synchronously, in-process — the demo/uv entrypoint.

Calls the exact same collection/correlation/persistence functions the Celery
tasks call (``dashboard.services``); the only difference from scaled mode is
that nothing goes through a task queue. With no credentials configured,
every connector runs against ``sample_data/`` fixtures.
"""

from django.core.management.base import BaseCommand, CommandError

from dashboard import services
from dashboard.config_db import build_app_config_from_db


class Command(BaseCommand):
    help = "Collect AD + agent inventories, correlate, and persist one CorrelationRun."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            help="Client slug (default: the first client, alphabetically).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Run for every client instead of just one.",
        )

    def handle(self, *args, **options):
        config = build_app_config_from_db()
        if not config.clients:
            raise CommandError(
                "No clients configured — run `manage.py import_config` (from "
                "config.yaml) or add one through the setup page first."
            )

        if options["all"]:
            slugs = sorted(config.clients)
        elif options["client"]:
            if options["client"] not in config.clients:
                raise CommandError(
                    f"Unknown client {options['client']!r}; "
                    f"configured: {', '.join(sorted(config.clients))}"
                )
            slugs = [options["client"]]
        else:
            slugs = [sorted(config.clients)[0]]

        for slug in slugs:
            run = services.run_pipeline_for_client(config, config.client(slug))
            summary = ", ".join(
                f"{name}={state}" for name, state in sorted(run.vendor_status.items())
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{slug}] run {run.pk}: {run.status} "
                    f"({run.snapshots.count()} snapshots; {summary})"
                )
            )
