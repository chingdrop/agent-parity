"""Tests for parsing raw Export-ADDevices.ps1 CSV output."""

import pandas as pd
import pytest

from agent_parity.ad_export import ADParseError, concat_ad_frames, parse_ad_export

SAMPLE_CSV = """\
Name,DNSHostName,OperatingSystem,LastLogonTimestamp,Enabled,DistinguishedName
ACME-WS-001,acme-ws-001.corp.acme.example,Windows 11 Enterprise,2026-07-01T08:30:00+00:00,True,"CN=ACME-WS-001,OU=Workstations,DC=corp,DC=acme,DC=example"
  ACME-WS-002  ,acme-ws-002.corp.acme.example,Windows 10 Enterprise,2026-06-01T10:00:00+00:00,False,"CN=ACME-WS-002,OU=Workstations,DC=corp,DC=acme,DC=example"
ACME-DC01,acme-dc01.corp.acme.example,Windows Server 2022 Datacenter,,True,"CN=ACME-DC01,OU=Domain Controllers,DC=corp,DC=acme,DC=example"
"""

SAMPLE_CSV_WITH_BUILD = """\
Name,DNSHostName,OperatingSystem,OperatingSystemVersion,LastLogonTimestamp,Enabled,DistinguishedName
ACME-WS-001,acme-ws-001.corp.acme.example,Windows 11 Enterprise,10.0 (22631),2026-07-01T08:30:00+00:00,True,"CN=ACME-WS-001,OU=Workstations,DC=corp,DC=acme,DC=example"
ACME-DC01,acme-dc01.corp.acme.example,Windows Server 2022 Datacenter,10.0 (20348),2026-07-01T08:30:00+00:00,True,"CN=ACME-DC01,OU=Domain Controllers,DC=corp,DC=acme,DC=example"
ACME-OLD01,acme-old01.corp.acme.example,Windows Server 2008 R2 Standard,,2026-07-01T08:30:00+00:00,True,"CN=ACME-OLD01,OU=Servers,DC=corp,DC=acme,DC=example"
"""


def test_parses_rows_and_normalizes_join_key():
    frame = parse_ad_export(SAMPLE_CSV)
    assert len(frame) == 3
    # Lowercased, whitespace-trimmed, no domain suffix.
    assert frame["join_key"].tolist() == ["acme-ws-001", "acme-ws-002", "acme-dc01"]
    # Original hostname preserved (trimmed).
    assert frame.loc[1, "hostname"] == "ACME-WS-002"


def test_timestamps_are_tz_aware_and_missing_becomes_nat():
    frame = parse_ad_export(SAMPLE_CSV)
    # tz-aware datetime dtype (unit resolution varies by pandas version).
    dtype = frame["last_logon"].dtype
    assert isinstance(dtype, pd.DatetimeTZDtype)
    assert str(dtype.tz) == "UTC"
    assert frame["last_logon"].isna().tolist() == [False, False, True]


def test_enabled_parsed_as_bool():
    frame = parse_ad_export(SAMPLE_CSV)
    assert frame["enabled"].tolist() == [True, False, True]


def test_rejects_output_without_name_column():
    with pytest.raises(ADParseError):
        parse_ad_export("Oops,Something\nbroke,badly\n")


def test_drops_rows_with_empty_hostname():
    csv_text = "Name,Enabled\nACME-WS-001,True\n,True\n"
    frame = parse_ad_export(csv_text)
    assert frame["join_key"].tolist() == ["acme-ws-001"]


def test_os_build_extracted_from_operating_system_version():
    frame = parse_ad_export(SAMPLE_CSV_WITH_BUILD)
    by_key = frame.set_index("join_key")["os_build"]
    assert by_key["acme-ws-001"] == 22631
    assert by_key["acme-dc01"] == 20348
    assert pd.isna(by_key["acme-old01"])


def test_missing_operating_system_version_column_yields_no_os_build():
    """Older-shaped CSV output (before this column existed) must still
    parse — os_build just comes back empty, not an error."""
    frame = parse_ad_export(SAMPLE_CSV)
    assert frame["os_build"].isna().all()


# --- concat_ad_frames: multi-domain master list -----------------------------------

SECOND_DOMAIN_CSV = """\
Name,DNSHostName,OperatingSystem,LastLogonTimestamp,Enabled,DistinguishedName
GLOBEX-BR-WS01,globex-br-ws01.br.globex.example,Windows 11 Enterprise,2026-07-01T10:00:00+00:00,True,"CN=GLOBEX-BR-WS01,OU=Workstations,DC=br,DC=globex,DC=example"
"""


def test_concat_ad_frames_combines_rows_from_every_domain():
    frame = concat_ad_frames([parse_ad_export(SAMPLE_CSV), parse_ad_export(SECOND_DOMAIN_CSV)])
    assert len(frame) == 4
    assert set(frame["join_key"]) == {
        "acme-ws-001",
        "acme-ws-002",
        "acme-dc01",
        "globex-br-ws01",
    }


def test_concat_ad_frames_of_a_single_domain_is_unchanged():
    """A single-domain client's collect_ad_frame still goes through
    concat_ad_frames — with one frame, the result must be identical."""
    original = parse_ad_export(SAMPLE_CSV)
    result = concat_ad_frames([original])
    pd.testing.assert_frame_equal(result, original)
