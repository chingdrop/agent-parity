"""Tests for dashboard/views.py — previously zero coverage (only manually
verified in-browser during development). Uses Django's test client against a
DB seeded via the real pipeline (services.run_pipeline_for_client), not
hand-built fixtures, so these tests exercise the same data shape the app
actually persists.
"""

import json

import pytest
from dashboard import services
from dashboard.models import CorrelationRun
from django.urls import reverse

from agent_parity.config import load_config
from agent_parity.models import CoverageStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def acme_run() -> CorrelationRun:
    config = load_config()
    return services.run_pipeline_for_client(config, config.client("acme"))


# --- overview --------------------------------------------------------------------


def test_overview_with_no_clients_shows_empty_state(client):
    response = client.get(reverse("dashboard:overview"))
    assert response.status_code == 200
    assert b"No clients yet" in response.content


def test_overview_shows_coverage_card_after_a_run(client, acme_run):
    response = client.get(reverse("dashboard:overview"))
    assert response.status_code == 200
    assert b"Acme Corp" in response.content
    cards = response.context["cards"]
    assert len(cards) == 1
    assert cards[0]["run"].pk == acme_run.pk
    assert cards[0]["coverage_pct"] is not None
    # Every vendor that reported at least one matched device shows up.
    vendor_names = {v["name"] for v in cards[0]["vendors"]}
    assert vendor_names == {"sentinelone", "carbonblack", "bitdefender"}


def test_overview_omits_inactive_clients(client, acme_run):
    acme_run.client.is_active = False
    acme_run.client.save()
    response = client.get(reverse("dashboard:overview"))
    assert response.context["cards"] == []


# --- device_list -----------------------------------------------------------------


def test_device_list_returns_rows_after_a_run(client, acme_run):
    response = client.get(reverse("dashboard:device_list"))
    assert response.status_code == 200
    assert response.context["page"].paginator.count == acme_run.snapshots.count()


def test_device_list_filters_by_status(client, acme_run):
    response = client.get(reverse("dashboard:device_list"), {"status": "orphaned_agent"})
    assert response.status_code == 200
    page = response.context["page"]
    assert len(page.object_list) > 0
    assert all(s.status == CoverageStatus.ORPHANED_AGENT for s in page.object_list)


def test_device_list_filters_by_vendor(client, acme_run):
    response = client.get(reverse("dashboard:device_list"), {"vendor": "bitdefender"})
    page = response.context["page"]
    assert len(page.object_list) > 0
    assert all(s.vendor == "bitdefender" for s in page.object_list)


def test_device_list_filters_by_client_slug(client, acme_run):
    response = client.get(reverse("dashboard:device_list"), {"client": "acme"})
    page = response.context["page"]
    assert len(page.object_list) > 0
    assert all(s.device.client.slug == "acme" for s in page.object_list)


def test_device_list_unknown_filter_values_yield_no_rows(client, acme_run):
    response = client.get(reverse("dashboard:device_list"), {"vendor": "not-a-real-vendor"})
    assert response.context["page"].paginator.count == 0


def test_device_list_paginates_at_fifty(client, acme_run):
    response = client.get(reverse("dashboard:device_list"))
    page = response.context["page"]
    assert page.paginator.per_page == 50
    assert len(page.object_list) <= 50


# --- device_detail -----------------------------------------------------------------


def test_device_detail_shows_history_for_a_real_device(client, acme_run):
    snapshot = acme_run.snapshots.first()
    response = client.get(reverse("dashboard:device_detail", args=[snapshot.device.pk]))
    assert response.status_code == 200
    assert response.context["device"].pk == snapshot.device.pk
    assert response.context["history"].count() >= 1


def test_device_detail_404s_for_unknown_device(client):
    response = client.get(reverse("dashboard:device_detail", args=[999999]))
    assert response.status_code == 404


# --- trend_data --------------------------------------------------------------------


def test_trend_data_returns_json_with_one_point_per_run(client, acme_run):
    response = client.get(reverse("dashboard:trend_data", args=["acme"]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"

    payload = json.loads(response.content)
    assert payload["client"] == "acme"
    assert len(payload["labels"]) == 1
    assert len(payload["coverage_pct"]) == 1
    assert 0 <= payload["coverage_pct"][0] <= 100


def test_trend_data_404s_for_unknown_client_slug(client):
    response = client.get(reverse("dashboard:trend_data", args=["nonexistent"]))
    assert response.status_code == 404


def test_trend_data_empty_before_any_run(client):
    from dashboard.models import Client

    Client.objects.create(name="Empty Co", slug="emptyco")
    response = client.get(reverse("dashboard:trend_data", args=["emptyco"]))
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload == {"client": "emptyco", "labels": [], "coverage_pct": []}
