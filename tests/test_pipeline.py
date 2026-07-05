"""Tests for agent_parity/pipeline.py: the collection helpers (multi-domain
concatenation and partial-failure tolerance) and the top-level
run_correlation/correlate_from_csvs orchestration.
"""

from dataclasses import replace
from datetime import datetime, timezone

from agent_parity.config import load_config
from agent_parity.models import CoverageStatus
from agent_parity.pipeline import (
    collect_ad_frame,
    collect_vendor_inventory,
    correlate_from_csvs,
    run_correlation,
)

# --- collect_ad_frame: multi-domain concatenation + partial-failure tolerance ---


def test_collect_ad_frame_concatenates_two_domains():
    """A second, real fixture file (a separate domain controller's export)
    must be collected and concatenated into one master frame alongside the
    default demo domain."""
    config = replace(load_config(), ad_target_devices=("ACME-DC01", "GLOBEX-BR-DC01"))
    ad_df, status = collect_ad_frame(config)

    assert ad_df is not None
    assert status == {"ad:ACME-DC01": "ok", "ad:GLOBEX-BR-DC01": "ok"}
    join_keys = set(ad_df["join_key"])
    assert "acme-dc01" in join_keys  # from the primary domain
    assert "globex-br-ws01" in join_keys  # from the second domain


def test_collect_ad_frame_tolerates_one_domain_failing():
    """One domain's export failing (bad target device, unreachable DC, ...)
    must not stop the others — same tolerance as vendor collection."""
    config = replace(load_config(), ad_target_devices=("ACME-DC01", "NONEXISTENT-DC99"))
    ad_df, status = collect_ad_frame(config)

    assert ad_df is not None
    assert status["ad:ACME-DC01"] == "ok"
    assert status["ad:NONEXISTENT-DC99"].startswith("error")
    assert "acme-dc01" in set(ad_df["join_key"])


def test_collect_ad_frame_returns_none_when_every_domain_fails():
    config = replace(load_config(), ad_target_devices=("NONEXISTENT-DC98", "NONEXISTENT-DC99"))
    ad_df, status = collect_ad_frame(config)

    assert ad_df is None
    assert all(v.startswith("error") for v in status.values())


# --- collect_vendor_inventory ------------------------------------------------------


def test_collect_vendor_inventory_returns_fixture_records():
    config = load_config()
    records, status = collect_vendor_inventory(config)

    assert status == {"sentinelone": "ok"}
    assert records
    assert all(r.vendor == "sentinelone" for r in records)


def test_collect_vendor_inventory_reports_failure_without_raising(tmp_path, monkeypatch):
    # An empty fixture directory has no inventory file for any vendor —
    # fetch_inventory() raises ConnectorError, which must surface as a
    # status entry, not propagate as an exception.
    monkeypatch.setattr("agent_parity.config.SAMPLE_DATA_DIR", tmp_path)
    config = load_config()

    records, status = collect_vendor_inventory(config)

    assert records == []
    assert status["sentinelone"].startswith("error")


# --- run_correlation ---------------------------------------------------------------


def test_run_correlation_returns_a_classified_result():
    config = load_config()
    result, vendor_status = run_correlation(config)

    assert result is not None
    assert all(state == "ok" for state in vendor_status.values())
    statuses = set(result.frame["status"])
    assert statuses == {s.value for s in CoverageStatus}


def test_run_correlation_returns_none_when_every_ad_domain_fails():
    config = replace(load_config(), ad_target_devices=("NONEXISTENT-DC99",))
    result, vendor_status = run_correlation(config)

    assert result is None
    assert vendor_status["ad:NONEXISTENT-DC99"].startswith("error")


# --- correlate_from_csvs: zero-config, no connectors, no sample_data --------------

_NOW = datetime.now(timezone.utc).isoformat()

_AD_CSV = f"""\
Name,DNSHostName,OperatingSystem,LastLogonTimestamp,Enabled,DistinguishedName
CORP-WS-001,corp-ws-001.corp.example,Windows 11 Enterprise,{_NOW},True,"CN=CORP-WS-001,OU=Workstations,DC=corp,DC=example"
CORP-WS-002,corp-ws-002.corp.example,Windows 11 Enterprise,{_NOW},True,"CN=CORP-WS-002,OU=Workstations,DC=corp,DC=example"
CORP-DC01,corp-dc01.corp.example,Windows Server 2022 Datacenter,{_NOW},True,"CN=CORP-DC01,OU=Domain Controllers,DC=corp,DC=example"
"""

_AGENT_CSV = f"""\
hostname,os,vendor,agent_id,last_seen,platform,machine_type
CORP-WS-001,Windows 11 Enterprise,crowdstrike,1,{_NOW},windows,desktop
CORP-DC01,Windows Server 2022 Datacenter,crowdstrike,2,{_NOW},windows,server
CORP-WS-999,Windows 11 Enterprise,crowdstrike,3,{_NOW},windows,desktop
"""


def test_correlate_from_csvs_classifies_with_no_config_or_connector():
    """Deliberately not touching sample_data/ or config.yaml at all — this
    path must work from two hand-rolled CSVs alone."""
    result = correlate_from_csvs(_AD_CSV, _AGENT_CSV)
    frame = result.frame

    def status(join_key):
        return set(frame.loc[frame["join_key"] == join_key, "status"])

    assert status("corp-ws-001") == {CoverageStatus.COVERED}
    assert status("corp-dc01") == {CoverageStatus.COVERED}
    assert status("corp-ws-002") == {CoverageStatus.MISSING_AGENT}
    assert status("corp-ws-999") == {CoverageStatus.ORPHANED_AGENT}
