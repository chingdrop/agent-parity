"""Tests for dashboard/services.py internals not already covered by
test_pipeline_sync.py (full pipeline runs) or test_tasks.py (the Celery
idempotency path): the small helpers and the sync-path persistence
idempotency guarantee outside of Celery entirely.
"""

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
