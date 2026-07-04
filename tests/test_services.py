"""Tests for dashboard/services.py internals not already covered by
test_pipeline_sync.py (full pipeline runs) or test_tasks.py (the Celery
idempotency path): the small helpers and the sync-path persistence
idempotency guarantee outside of Celery entirely.
"""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from agent_parity.config import load_config
from agent_parity.correlation.engine import CorrelationResult
from dashboard import services
from dashboard.models import Client, CorrelationRun, CoverageSnapshot

pytestmark = pytest.mark.django_db


# --- _first_valid --------------------------------------------------------------


def test_first_valid_returns_first_non_null():
    assert services._first_valid(None, "b", "c") == "b"


def test_first_valid_skips_nan():
    assert services._first_valid(np.nan, None, "c") == "c"


def test_first_valid_returns_none_when_all_missing():
    assert services._first_valid(None, np.nan, None) is None


def test_first_valid_returns_first_value_even_if_falsy_but_not_null():
    # 0 and "" are valid values, not "missing" — only None/NaN count as absent.
    assert services._first_valid(0, "fallback") == 0


# --- sync_client_from_config: upsert semantics ----------------------------------


def test_sync_client_from_config_creates_new_client():
    config = load_config()
    client = services.sync_client_from_config(config.client("acme"))
    assert client.slug == "acme"
    assert client.name == "Acme Corp"
    assert Client.objects.count() == 1


def test_sync_client_from_config_updates_existing_client_on_second_call():
    config = load_config()
    client_cfg = config.client("acme")

    first = services.sync_client_from_config(client_cfg)
    Client.objects.filter(pk=first.pk).update(name="Stale Name", enabled_vendors=[])

    second = services.sync_client_from_config(client_cfg)

    assert second.pk == first.pk
    assert second.name == "Acme Corp"
    assert second.enabled_vendors == sorted(client_cfg.vendors)
    assert Client.objects.count() == 1  # no duplicate row


# --- collect_ad_frame: multi-domain concatenation + partial-failure tolerance ---


def test_collect_ad_frame_concatenates_globexs_two_domains():
    """Globex declares two AD domains in config.yaml — both fixture exports
    must be collected and concatenated into one master frame."""
    config = load_config()
    ad_df, status = services.collect_ad_frame(config, "globex")

    assert ad_df is not None
    assert status == {"ad:GLOBEX-DC01": "ok", "ad:GLOBEX-BR-DC01": "ok"}
    join_keys = set(ad_df["join_key"])
    assert "globex-dc01" in join_keys  # from the primary domain
    assert "globex-br-ws01" in join_keys  # from the branch domain


def test_collect_ad_frame_tolerates_one_domain_failing():
    """One domain's export failing (bad target device, unreachable DC, ...)
    must not stop the others — same tolerance as per-vendor collection."""
    config = load_config()
    broken_globex = replace(
        config.client("globex"), ad_target_devices=("GLOBEX-DC01", "NONEXISTENT-DC99")
    )
    config = replace(config, clients={**config.clients, "globex": broken_globex})

    ad_df, status = services.collect_ad_frame(config, "globex")

    assert ad_df is not None
    assert status["ad:GLOBEX-DC01"] == "ok"
    assert status["ad:NONEXISTENT-DC99"].startswith("error")
    assert "globex-dc01" in set(ad_df["join_key"])


def test_collect_ad_frame_returns_none_when_every_domain_fails():
    config = load_config()
    broken_globex = replace(
        config.client("globex"), ad_target_devices=("NONEXISTENT-DC98", "NONEXISTENT-DC99")
    )
    config = replace(config, clients={**config.clients, "globex": broken_globex})

    ad_df, status = services.collect_ad_frame(config, "globex")

    assert ad_df is None
    assert all(v.startswith("error") for v in status.values())


# --- site_status_key -------------------------------------------------------------


def test_site_status_key_is_plain_vendor_name_for_a_single_site():
    assert services.site_status_key("sentinelone", {}, 0, 1) == "sentinelone"


def test_site_status_key_uses_an_explicit_label_when_present():
    assert services.site_status_key("carbonblack", {"label": "branch"}, 1, 2) == "carbonblack:branch"


def test_site_status_key_falls_back_to_index_when_unlabeled_but_multiple():
    assert services.site_status_key("carbonblack", {}, 0, 2) == "carbonblack:0"


# --- collect_vendor_inventory: multi-tenant concatenation + partial failure ------


def test_collect_vendor_inventory_concatenates_acmes_two_carbonblack_tenants():
    """Acme has two Carbon Black tenants in config.yaml — both fixture
    exports must be collected and concatenated."""
    config = load_config()
    records, status = services.collect_vendor_inventory(config, "acme", "carbonblack")

    assert status == {"carbonblack:0": "ok", "carbonblack:branch": "ok"}
    hostnames = {r.hostname for r in records}
    assert "ACME-DC02" in hostnames  # from the primary (unlabeled) tenant
    assert "ACME-BR-WS01" in hostnames  # from the branch tenant


def test_collect_vendor_inventory_tolerates_one_tenant_failing():
    config = load_config()
    broken_acme = replace(
        config.client("acme"),
        vendors={
            **config.client("acme").vendors,
            "carbonblack": (
                config.client("acme").vendors["carbonblack"][0],
                {**config.client("acme").vendors["carbonblack"][1], "label": "nonexistent"},
            ),
        },
    )
    config = replace(config, clients={**config.clients, "acme": broken_acme})

    records, status = services.collect_vendor_inventory(config, "acme", "carbonblack")

    assert status["carbonblack:0"] == "ok"
    assert status["carbonblack:nonexistent"].startswith("error")
    assert any(r.hostname == "ACME-DC02" for r in records)


# --- finalize_run: no-AD-data handling ------------------------------------------


def test_finalize_run_marks_the_run_failed_when_ad_df_is_none():
    config = load_config()
    client = services.sync_client_from_config(config.client("acme"))
    run = CorrelationRun.objects.create(client=client, stale_days=config.stale_days)

    count = services.finalize_run(
        run, None, [], {"ad:ACME-DC01": "error: target endpoint offline"}
    )

    run.refresh_from_db()
    assert count == 0
    assert run.status == CorrelationRun.RunStatus.FAILED
    assert run.snapshots.count() == 0


# --- persist_correlation idempotency, outside of Celery ------------------------


def _empty_result() -> CorrelationResult:
    frame = pd.DataFrame(
        columns=[
            "join_key", "hostname", "os", "vendor", "agent_id", "last_seen",
            "agent_version", "platform", "machine_type", "status", "match_method",
        ]
    )
    return CorrelationResult(frame=frame, summary={})


def test_persist_correlation_is_a_noop_on_an_already_finalized_run():
    """Mirrors test_tasks.py's Celery-level idempotency check, but exercises
    services.persist_correlation directly — the guarantee doesn't depend on
    going through a task at all."""
    config = load_config()
    client = services.sync_client_from_config(config.client("acme"))
    run = CorrelationRun.objects.create(
        client=client, stale_days=14, status=CorrelationRun.RunStatus.COMPLETE
    )

    count = services.persist_correlation(run, _empty_result(), {"ad": "ok"})

    assert count == 0
    assert CoverageSnapshot.objects.filter(run=run).count() == 0
