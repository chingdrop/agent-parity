"""Seed two runs of demo history from one fixture set.

The fixtures under ``sample_data/`` are a single point-in-time snapshot per
client. To give the trend chart and device-history pages something real to
show, this command runs the pipeline twice per client:

* **Run 1** — the fixtures as-authored, backdated one day.
* **Run 2** — the same fixtures passed through a deterministic *drift*
  transform that plays out a plausible day of change:

  1. one previously-uncovered AD device gets remediated (fresh agent),
  2. one covered device's agent goes quiet (drops past the stale threshold),
  3. one orphaned agent is decommissioned (disappears from inventory),
  4. one brand-new AD-only workstation appears (new coverage gap).

Deterministic on purpose: every transition the dashboard shows is one of the
four above, so the demo is reviewable rather than random.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone as dt_timezone

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from agent_parity.config import ConfigError, load_config
from agent_parity.models import AgentDevice, normalize_hostname
from dashboard import services
from dashboard.models import CorrelationRun


def demo_drift(ad_df: pd.DataFrame, agent_records: list[AgentDevice]):
    """Apply the four scripted day-two changes described in the module docstring."""
    now = datetime.now(dt_timezone.utc)
    records = sorted(agent_records, key=lambda r: (r.vendor, r.join_key))
    ad_keys = set(ad_df["join_key"])
    agent_keys = {r.join_key for r in records}

    # Pick the regression target (step 2) from the *original* inventory before
    # the remediation agent is appended — otherwise the brand-new agent, with
    # the freshest possible check-in, would immediately be the one to go stale.
    matched = [r for r in records if r.last_seen and r.join_key in ad_keys]
    freshest = max(matched, key=lambda r: (r.last_seen, r.join_key)) if matched else None

    # 1. Remediation: the first AD-only device gets a SentinelOne agent.
    missing = sorted(ad_keys - agent_keys)
    if missing:
        ad_row = ad_df[ad_df["join_key"] == missing[0]].iloc[0]
        records.append(
            AgentDevice(
                vendor="sentinelone",
                agent_id=f"s1-demo-{missing[0]}",
                hostname=str(ad_row["hostname"]),
                os=str(ad_row["os"]),
                last_seen=now,
                agent_version="24.2.3.471",
            )
        )

    # 2. Regression: the matched agent with the freshest check-in goes stale.
    if freshest is not None:
        records[records.index(freshest)] = replace(
            freshest, last_seen=now - timedelta(days=30)
        )

    # 3. Decommission: the first orphaned agent drops out of inventory.
    orphans = sorted(agent_keys - ad_keys)
    if orphans:
        records = [r for r in records if r.join_key != orphans[0]]

    # 4. A brand-new AD-only workstation appears.
    prefix = str(ad_df["hostname"].iloc[0]).split("-")[0]
    new_hostname = f"{prefix}-WS-NEW1"
    new_row = pd.DataFrame(
        [
            {
                "join_key": normalize_hostname(new_hostname),
                "hostname": new_hostname,
                "dns_hostname": "",
                "os": "Windows 11 Enterprise",
                "last_logon": pd.Timestamp(now),
                "enabled": True,
                "distinguished_name": f"CN={new_hostname},OU=Workstations",
            }
        ]
    )
    return pd.concat([ad_df, new_row], ignore_index=True), records


class Command(BaseCommand):
    help = "Seed two CorrelationRuns of demo history per client from the fixtures."

    def handle(self, *args, **options):
        try:
            config = load_config()
        except (ConfigError, FileNotFoundError) as exc:
            raise CommandError(f"Could not load config.yaml: {exc}") from exc

        for slug in sorted(config.clients):
            client_cfg = config.client(slug)

            # Run 1: fixtures as-authored, backdated a day.
            client = services.sync_client_from_config(client_cfg)
            run1 = CorrelationRun.objects.create(
                client=client,
                stale_days=config.stale_days,
                started_at=timezone.now() - timedelta(days=1),
            )
            services.run_pipeline_for_client(config, client_cfg, run=run1)

            # Run 2: the same fixtures, one scripted day of drift later.
            run2 = services.run_pipeline_for_client(config, client_cfg, drift=demo_drift)

            self.stdout.write(
                self.style.SUCCESS(
                    f"[{slug}] seeded runs {run1.pk} (backdated) and {run2.pk} (drifted): "
                    f"{run2.snapshots.count()} snapshots in latest run"
                )
            )
