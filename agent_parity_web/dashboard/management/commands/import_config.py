"""One-time bootstrap: import config.yaml/.env into the DB.

Client topology and vendor credentials are DB-backed now (see
``dashboard/config_db.py``) — config.yaml is only read at import time, not by
any running entrypoint. Idempotent: re-running this against an unchanged
config.yaml is a no-op (``import_app_config`` upserts). This is also what a
future setup-page YAML upload calls under the hood.
"""

from django.core.management.base import BaseCommand, CommandError

from agent_parity.config import ConfigError, load_config
from dashboard.config_db import import_app_config


class Command(BaseCommand):
    help = "Import config.yaml/.env into the DB (client topology + vendor credentials)."

    def handle(self, *args, **options):
        try:
            config = load_config()
        except (ConfigError, FileNotFoundError) as exc:
            raise CommandError(f"Could not load config.yaml: {exc}") from exc

        import_app_config(config)

        vendor_names = ", ".join(sorted(config.vendors))
        client_names = ", ".join(sorted(config.clients))
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(config.vendors)} vendor(s) [{vendor_names}] and "
                f"{len(config.clients)} client(s) [{client_names}] from config.yaml."
            )
        )
