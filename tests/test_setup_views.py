"""Tests for the setup page (dashboard/views_setup.py) — the manual
add/edit form and the one-time config.yaml upload, both gated behind
staff_member_required since they read/write vendor credentials.
"""

import io

import pytest
from django.urls import reverse

from dashboard.models import Client, VendorCredential

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="staff", password="pw", is_staff=True)
    client.force_login(user)
    return client


# --- auth gating -------------------------------------------------------------------


def test_setup_overview_redirects_anonymous_users_to_login(client):
    response = client.get(reverse("dashboard:setup_overview"))
    assert response.status_code == 302
    assert "/admin/login/" in response.url


# --- setup_overview -----------------------------------------------------------------


def test_setup_overview_lists_clients_and_global_vendor_status(staff_client):
    Client.objects.create(name="Acme Corp", slug="acme", enabled_vendors=["sentinelone"])
    VendorCredential.objects.create(
        client=None, vendor="sentinelone", credentials={"api_url": "x", "api_token": "y"}
    )

    response = staff_client.get(reverse("dashboard:setup_overview"))
    assert response.status_code == 200
    assert b"Acme Corp" in response.content
    global_vendors = {v["name"]: v["configured"] for v in response.context["global_vendors"]}
    assert global_vendors["sentinelone"] is True
    assert global_vendors["bitdefender"] is False


def test_setup_overview_treats_all_none_credentials_as_not_configured(staff_client):
    """A row can exist with every value None — an imported config.yaml whose
    ${VAR} refs were unset in the environment. That's still "not
    configured," the same as no row at all, not "yes"."""
    VendorCredential.objects.create(
        client=None, vendor="sentinelone", credentials={"api_url": None, "api_token": None}
    )

    response = staff_client.get(reverse("dashboard:setup_overview"))
    global_vendors = {v["name"]: v["configured"] for v in response.context["global_vendors"]}
    assert global_vendors["sentinelone"] is False


# --- client_form: create ------------------------------------------------------------


def test_client_create_get_renders_form(staff_client):
    response = staff_client.get(reverse("dashboard:client_create"))
    assert response.status_code == 200
    assert b"carbonblack credentials" in response.content


def test_client_create_post_creates_client_and_per_client_credentials(staff_client):
    response = staff_client.post(
        reverse("dashboard:client_create"),
        {
            "name": "New Client",
            "slug": "newco",
            "is_active": "on",
            "ad_target_devices": "NEWCO-DC01\nNEWCO-BR-DC01",
            "sync_interval_hours": "12",
            "enabled_vendors": ["sentinelone", "carbonblack"],
            "carbonblack-api_url": "https://defense.example.com",
            "carbonblack-api_id": "NEWID",
            "carbonblack-api_key": "newco-secret",
            "carbonblack-org_key": "NEWORG",
        },
    )
    assert response.status_code == 302

    client_row = Client.objects.get(slug="newco")
    assert client_row.ad_target_devices == ["NEWCO-DC01", "NEWCO-BR-DC01"]
    assert client_row.sync_interval_hours == 12
    assert set(client_row.enabled_vendors) == {"sentinelone", "carbonblack"}

    cred = VendorCredential.objects.get(client=client_row, vendor="carbonblack")
    assert cred.credentials == {
        "api_url": "https://defense.example.com",
        "api_id": "NEWID",
        "api_key": "newco-secret",
        "org_key": "NEWORG",
    }
    # Global vendor (sentinelone) is enabled but not touched by this form.
    assert not VendorCredential.objects.filter(client=client_row, vendor="sentinelone").exists()


# --- client_form: edit ---------------------------------------------------------------


def test_client_edit_blank_credential_fields_preserve_existing_values(staff_client):
    client_row = Client.objects.create(
        name="Acme Corp", slug="acme", enabled_vendors=["carbonblack"]
    )
    VendorCredential.objects.create(
        client=client_row,
        vendor="carbonblack",
        credentials={
            "api_url": "https://defense.example.com",
            "api_id": "ACMEID",
            "api_key": "acme-secret",
            "org_key": "ACMEORG",
        },
    )

    response = staff_client.post(
        reverse("dashboard:client_edit", args=["acme"]),
        {
            "name": "Acme Corp",
            "slug": "acme",
            "is_active": "on",
            "ad_target_devices": "ACME-DC01",
            "sync_interval_hours": "6",
            "enabled_vendors": ["carbonblack"],
            # Only org_key changes; everything else blank -> keeps its value.
            "carbonblack-api_url": "",
            "carbonblack-api_id": "",
            "carbonblack-api_key": "",
            "carbonblack-org_key": "ACMEORG-ROTATED",
        },
    )
    assert response.status_code == 302

    cred = VendorCredential.objects.get(client=client_row, vendor="carbonblack")
    assert cred.credentials["api_id"] == "ACMEID"
    assert cred.credentials["api_key"] == "acme-secret"
    assert cred.credentials["org_key"] == "ACMEORG-ROTATED"


def test_client_edit_never_echoes_stored_credentials_into_the_page(staff_client):
    client_row = Client.objects.create(
        name="Acme Corp", slug="acme", enabled_vendors=["carbonblack"]
    )
    VendorCredential.objects.create(
        client=client_row, vendor="carbonblack", credentials={"api_key": "super-secret-value"}
    )

    response = staff_client.get(reverse("dashboard:client_edit", args=["acme"]))
    assert b"super-secret-value" not in response.content


def test_client_edit_with_multiple_tenants_edits_the_first_without_crashing(staff_client):
    """A client with more than one VendorCredential row for the same vendor
    (multiple Carbon Black tenants) must not raise MultipleObjectsReturned —
    the form edits only the first (by site_label/pk order); the second
    tenant's row is left untouched."""
    client_row = Client.objects.create(
        name="Acme Corp", slug="acme", enabled_vendors=["carbonblack"]
    )
    first = VendorCredential.objects.create(
        client=client_row, vendor="carbonblack", site_label="0", credentials={"org_key": "FIRST"}
    )
    second = VendorCredential.objects.create(
        client=client_row,
        vendor="carbonblack",
        site_label="branch",
        credentials={"org_key": "SECOND"},
    )

    response = staff_client.post(
        reverse("dashboard:client_edit", args=["acme"]),
        {
            "name": "Acme Corp",
            "slug": "acme",
            "is_active": "on",
            "ad_target_devices": "ACME-DC01",
            "sync_interval_hours": "6",
            "enabled_vendors": ["carbonblack"],
            "carbonblack-api_url": "",
            "carbonblack-api_id": "",
            "carbonblack-api_key": "",
            "carbonblack-org_key": "FIRST-ROTATED",
        },
    )
    assert response.status_code == 302
    assert VendorCredential.objects.filter(client=client_row, vendor="carbonblack").count() == 2

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.credentials["org_key"] == "FIRST-ROTATED"
    assert second.credentials["org_key"] == "SECOND"


# --- vendor_credential_form (global) -------------------------------------------------


def test_vendor_credential_form_rejects_per_client_vendor(staff_client):
    response = staff_client.get(reverse("dashboard:vendor_credential_form", args=["carbonblack"]))
    assert response.status_code == 404


def test_vendor_credential_form_post_merges_over_existing_values(staff_client):
    VendorCredential.objects.create(
        client=None,
        vendor="sentinelone",
        credentials={"api_url": "https://usea1.sentinelone.net", "api_token": "old-token"},
    )

    response = staff_client.post(
        reverse("dashboard:vendor_credential_form", args=["sentinelone"]),
        {"api_url": "", "api_token": "new-token"},
    )
    assert response.status_code == 302

    cred = VendorCredential.objects.get(client=None, vendor="sentinelone")
    assert cred.credentials["api_url"] == "https://usea1.sentinelone.net"
    assert cred.credentials["api_token"] == "new-token"


# --- import_config_yaml --------------------------------------------------------------


VALID_YAML = b"""
stale_days: 14
vendors:
  sentinelone:
    scope: global
    api_url: https://usea1.sentinelone.net
    api_token: uploaded-token
clients:
  - name: Uploaded Co
    slug: uploadedco
    ad_target_devices:
      - UPLOADEDCO-DC01
    sync_interval_hours: 8
    vendors:
      sentinelone: {}
"""


def test_import_config_yaml_creates_client_and_credentials(staff_client):
    upload = io.BytesIO(VALID_YAML)
    upload.name = "config.yaml"

    response = staff_client.post(
        reverse("dashboard:import_config_yaml"), {"config_file": upload}, format="multipart"
    )
    assert response.status_code == 302

    client_row = Client.objects.get(slug="uploadedco")
    assert client_row.ad_target_devices == ["UPLOADEDCO-DC01"]
    cred = VendorCredential.objects.get(client=None, vendor="sentinelone")
    assert cred.credentials["api_token"] == "uploaded-token"


def test_import_config_yaml_malformed_file_becomes_a_form_error_not_a_500(staff_client):
    upload = io.BytesIO(b"not: valid: yaml: [")
    upload.name = "config.yaml"

    response = staff_client.post(
        reverse("dashboard:import_config_yaml"), {"config_file": upload}, format="multipart"
    )
    assert response.status_code == 200
    assert b"Could not parse this file" in response.content
    assert Client.objects.count() == 0
