"""Tests for dashboard/config_db.py — the symmetric import_app_config /
build_app_config_from_db pair that replaced load_config() as the source of
truth for every production entrypoint. These prove the round trip: an
AppConfig built from the DB must behave identically to one parsed straight
from config.yaml for everything the pipeline actually uses
(sites_for/get_connectors/pick_ad_export_vendor), since agent_parity/
itself never learns the DB exists.
"""

import pytest

from agent_parity.config import ConfigError, load_config
from dashboard.config_db import build_app_config_from_db, import_app_config
from dashboard.models import Client, VendorCredential

pytestmark = pytest.mark.django_db


@pytest.fixture
def imported():
    import_app_config(load_config())
    return build_app_config_from_db()


def test_import_creates_a_client_row_per_config_yaml_client(imported):
    assert set(Client.objects.values_list("slug", flat=True)) == {"acme", "globex"}


def test_import_carries_topology_fields_onto_the_client_row():
    import_app_config(load_config())
    acme = Client.objects.get(slug="acme")
    assert acme.ad_target_devices == ["ACME-DC01"]
    assert acme.sync_interval_hours == 6


def test_global_vendor_gets_one_credential_row_per_named_account(imported):
    """SentinelOne has two named accounts in config.yaml (mssp/dfir) — both
    are global (client=None) rows, distinguished by site_label doubling as
    the account name."""
    rows = VendorCredential.objects.filter(vendor="sentinelone", client__isnull=True)
    assert {r.site_label for r in rows} == {"mssp", "dfir"}
    assert all(r.client is None for r in rows)


def test_global_vendor_with_a_single_account_still_gets_a_named_row(imported):
    # BitDefender only has one account ("default") but it's still named,
    # not blank — accounts are always named, unlike per-client site labels.
    row = VendorCredential.objects.get(vendor="bitdefender", client__isnull=True)
    assert row.site_label == "default"


def test_per_client_vendor_gets_one_row_per_enabled_client(imported):
    # BitDefender is global, not per-client — only Carbon Black (acme only,
    # per config.yaml) should produce per-client rows: two, since acme has
    # two Carbon Black tenants (its primary org plus a "branch" one).
    rows = VendorCredential.objects.filter(vendor="carbonblack")
    assert [r.client.slug for r in rows] == ["acme", "acme"]
    # The unlabeled tenant still gets a storage-identity site_label ("0")
    # since there's more than one row to distinguish — but that's purely
    # internal bookkeeping, never surfaced as a "label" (see
    # build_app_config_from_db and its dedicated round-trip test).
    assert {r.site_label for r in rows} == {"0", "branch"}


def test_build_app_config_from_db_round_trips_client_topology(imported):
    acme = imported.client("acme")
    assert acme.name == "Acme Corp"
    assert acme.ad_target_devices == ("ACME-DC01",)
    assert acme.sync_interval_hours == 6
    assert set(acme.vendors) == {"sentinelone", "carbonblack", "bitdefender"}


def test_build_app_config_from_db_round_trips_two_carbonblack_tenants(imported):
    """Acme's two CB tenants must both come back as independent credential
    blocks, and — critically — the unlabeled (primary) one must not gain a
    spurious "label" from its storage-only site_label ("0"): VendorCredential
    auto-assigns an index for DB row identity when there's more than one row,
    but that index is never real config, and reintroducing it as a "label"
    previously broke fixture-file lookup (fixture mode picks
    ad_export/inventory files by label — see connectors/base.py)."""
    sites = imported.client("acme").vendors["carbonblack"]
    assert len(sites) == 2
    assert "label" not in sites[0]
    assert sites[1]["label"] == "branch"


def test_build_app_config_from_db_round_trips_multiple_domains(imported):
    globex = imported.client("globex")
    assert globex.ad_target_devices == ("GLOBEX-DC01", "GLOBEX-BR-DC01")


def test_build_app_config_from_db_resolves_credentials_the_same_way_as_yaml(monkeypatch):
    monkeypatch.setenv("SENTINELONE_MSSP_API_URL", "https://usea1.sentinelone.net")
    monkeypatch.setenv("SENTINELONE_MSSP_API_TOKEN", "s1-global-token")
    monkeypatch.setenv("ACME_CB_API_URL", "https://defense.conferdeploy.net")
    monkeypatch.setenv("ACME_CB_API_ID", "ACMEID")
    monkeypatch.setenv("ACME_CB_API_KEY", "acme-cb-secret")
    monkeypatch.setenv("ACME_CB_ORG_KEY", "ACMEORG")

    import_app_config(load_config())
    config = build_app_config_from_db()

    assert config.sites_for("acme", "sentinelone") == config.sites_for("globex", "sentinelone")
    assert config.sites_for("acme", "sentinelone")[0]["api_token"] == "s1-global-token"
    creds = config.sites_for("acme", "carbonblack")[0]
    assert creds["api_id"] == "ACMEID"
    assert creds["org_key"] == "ACMEORG"


def test_build_app_config_from_db_round_trips_a_clients_chosen_account(imported):
    """Both demo clients pick the "mssp" SentinelOne account in config.yaml —
    that choice must survive the DB round-trip as an "account" key."""
    acme_site = imported.client("acme").vendors["sentinelone"][0]
    globex_site = imported.client("globex").vendors["sentinelone"][0]
    assert acme_site["account"] == "mssp"
    assert globex_site["account"] == "mssp"


def test_client_without_vendor_enabled_is_still_rejected_the_same_way(imported):
    # Globex doesn't declare carbonblack at all — same ConfigError as the
    # YAML-backed path (tests/test_config.py).
    with pytest.raises(ConfigError, match="does not enable"):
        imported.sites_for("globex", "carbonblack")


def test_reimporting_an_unchanged_config_is_idempotent():
    import_app_config(load_config())
    client_count = Client.objects.count()
    credential_count = VendorCredential.objects.count()

    import_app_config(load_config())

    assert Client.objects.count() == client_count
    assert VendorCredential.objects.count() == credential_count


def test_vendor_with_no_credential_row_yet_still_resolves_to_an_empty_dict():
    """A fresh DB (nothing imported, nothing added through the setup page)
    must not make sites_for() raise for a known vendor — an unset
    credential is exactly what puts a connector into fixture mode."""
    Client.objects.create(name="Acme Corp", slug="acme", enabled_vendors=["sentinelone"])
    config = build_app_config_from_db()
    assert config.sites_for("acme", "sentinelone") == ({},)
