"""Tests for the Django ORM schema itself (dashboard/models.py) — __str__
methods, constraints, and the CoverageStatus choices staying in lockstep
with the pipeline's own enum. Not exercised as a dedicated file elsewhere;
only indirectly through pipeline runs in test_pipeline_sync.py/test_tasks.py.
"""

import pytest
from django.db import IntegrityError, transaction

from agent_parity.models import CoverageStatus as PipelineCoverageStatus
from dashboard.models import (
    Client,
    CorrelationRun,
    CoverageSnapshot,
    CoverageStatus,
    Device,
    VendorCredential,
)

pytestmark = pytest.mark.django_db


def test_coverage_status_choices_match_pipeline_enum():
    """The ORM's CoverageStatus is a hand-maintained mirror of the pipeline's
    — if they drift, persisted status strings silently stop round-tripping
    through the choices field."""
    orm_values = {choice.value for choice in CoverageStatus}
    pipeline_values = {status.value for status in PipelineCoverageStatus}
    assert orm_values == pipeline_values


def test_client_str_is_name():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    assert str(client) == "Acme Corp"


def test_device_str_includes_client_slug_and_join_key():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    device = Device.objects.create(client=client, join_key="acme-ws-001", hostname="ACME-WS-001")
    assert str(device) == "acme/acme-ws-001"


def test_device_unique_per_client_but_not_across_clients():
    acme = Client.objects.create(name="Acme Corp", slug="acme")
    globex = Client.objects.create(name="Globex", slug="globex")
    Device.objects.create(client=acme, join_key="dc01", hostname="DC01")

    # Same join_key, different client: fine.
    Device.objects.create(client=globex, join_key="dc01", hostname="DC01")

    # Same join_key, same client: violates the constraint.
    with pytest.raises(IntegrityError), transaction.atomic():
        Device.objects.create(client=acme, join_key="dc01", hostname="DC01-dup")


def test_correlation_run_str_includes_client_and_timestamp():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    run = CorrelationRun.objects.create(client=client)
    assert str(run) == f"acme run {run.pk} ({run.started_at:%Y-%m-%d %H:%M})"


def test_correlation_run_defaults_to_pending():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    run = CorrelationRun.objects.create(client=client)
    assert run.status == CorrelationRun.RunStatus.PENDING
    assert run.stale_days == 14
    assert run.vendor_status == {}


def test_coverage_snapshot_str_shows_vendor_or_placeholder():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    device = Device.objects.create(client=client, join_key="ws-01", hostname="WS-01")
    run = CorrelationRun.objects.create(client=client)

    covered = CoverageSnapshot.objects.create(
        run=run, device=device, status=CoverageStatus.COVERED, vendor="sentinelone"
    )
    assert str(covered) == "ws-01 [sentinelone] covered"

    missing = CoverageSnapshot.objects.create(
        run=run, device=device, status=CoverageStatus.MISSING_AGENT, vendor=""
    )
    assert str(missing) == "ws-01 [no vendor] missing_agent"


def test_coverage_snapshot_platform_and_machine_type_default_blank():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    device = Device.objects.create(client=client, join_key="ws-01", hostname="WS-01")
    run = CorrelationRun.objects.create(client=client)
    snapshot = CoverageSnapshot.objects.create(
        run=run, device=device, status=CoverageStatus.MISSING_AGENT
    )
    assert snapshot.platform == ""
    assert snapshot.machine_type == ""


def test_deleting_client_cascades_to_devices_and_runs():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    Device.objects.create(client=client, join_key="ws-01", hostname="WS-01")
    CorrelationRun.objects.create(client=client)

    client.delete()

    assert Device.objects.count() == 0
    assert CorrelationRun.objects.count() == 0


def test_client_topology_fields_default():
    client = Client.objects.create(name="Acme Corp", slug="acme")
    assert client.ad_target_devices == []
    assert client.sync_interval_hours == 24


def test_vendor_credential_str_shows_global_or_client_scope():
    global_cred = VendorCredential.objects.create(
        client=None, vendor="sentinelone", credentials={"api_url": "x", "api_token": "y"}
    )
    assert str(global_cred) == "global/sentinelone"

    client = Client.objects.create(name="Acme Corp", slug="acme")
    per_client_cred = VendorCredential.objects.create(
        client=client, vendor="carbonblack", credentials={"api_id": "ACMEID"}
    )
    assert str(per_client_cred) == "acme/carbonblack"


def test_vendor_credential_round_trips_through_encryption():
    creds = {"api_url": "https://usea1.sentinelone.net", "api_token": "s1-secret"}
    VendorCredential.objects.create(client=None, vendor="sentinelone", credentials=creds)

    fetched = VendorCredential.objects.get(vendor="sentinelone")
    assert fetched.credentials == creds


def test_vendor_credential_is_encrypted_at_rest():
    """The raw DB column must never contain the plaintext credential values —
    only dashboard/fields.py's EncryptedJSONField should ever see them
    decrypted."""
    from django.db import connection

    VendorCredential.objects.create(
        client=None, vendor="bitdefender", credentials={"api_key": "super-secret-value"}
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT credentials FROM dashboard_vendorcredential")
        raw_value = cursor.fetchone()[0]
    assert "super-secret-value" not in raw_value


def test_vendor_credential_only_one_global_row_per_vendor():
    VendorCredential.objects.create(client=None, vendor="sentinelone", credentials={})
    with pytest.raises(IntegrityError), transaction.atomic():
        VendorCredential.objects.create(client=None, vendor="sentinelone", credentials={})


def test_vendor_credential_one_row_per_client_per_vendor_but_not_across_clients():
    acme = Client.objects.create(name="Acme Corp", slug="acme")
    globex = Client.objects.create(name="Globex", slug="globex")
    VendorCredential.objects.create(client=acme, vendor="carbonblack", credentials={})

    # Same vendor, different client: fine.
    VendorCredential.objects.create(client=globex, vendor="carbonblack", credentials={})

    # Same vendor, same client: violates the constraint.
    with pytest.raises(IntegrityError), transaction.atomic():
        VendorCredential.objects.create(client=acme, vendor="carbonblack", credentials={})
