"""Run the full pipeline synchronously, in-process — the demo/uv entrypoint.

Calls the exact same collection/correlation/persistence functions the Celery
tasks call (``dashboard.services``); the only difference from scaled mode is
that nothing goes through a task queue. With no credentials in the
environment, every connector runs against ``sample_data/`` fixtures.
"""

from django.core.management.base import BaseCommand, CommandError

from agent_parity.config import ConfigError, load_config
from dashboard import services


class Command(BaseCommand):
    help = "Collect AD + agent inventories, correlate, and persist one CorrelationRun."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client",
            help="Client slug from config.yaml (default: the first client).",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Run for every client in config.yaml instead of just one.",
        )

    def handle(self, *args, **options):
        try:
            config = load_config()
        except (ConfigError, FileNotFoundError) as exc:
            raise CommandError(f"Could not load config.yaml: {exc}") from exc
        if not config.clients:
            raise CommandError("config.yaml declares no clients.")

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
