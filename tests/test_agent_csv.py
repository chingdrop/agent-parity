"""Tests for parsing a generic, vendor-agnostic agent/EDR inventory CSV."""

import pandas as pd
import pytest

from agent_parity.agent_csv import AgentCSVParseError, parse_agent_csv

FULL_CSV = """\
hostname,os,os_build,vendor,agent_id,last_seen,agent_version,platform,machine_type
ACME-WS-001,Windows 11 Enterprise,22631,crowdstrike,abc123,2026-07-01T08:30:00+00:00,7.1.0,windows,desktop
  ACME-DC01  ,Windows Server 2022 Datacenter,20348,crowdstrike,def456,2026-07-01T09:00:00+00:00,7.1.0,windows,server
"""

MINIMAL_CSV = "hostname\nACME-WS-001\nACME-DC01\n"


def test_full_csv_parses_every_column():
    frame = parse_agent_csv(FULL_CSV)
    assert len(frame) == 2
    assert frame["join_key"].tolist() == ["acme-ws-001", "acme-dc01"]
    assert frame.loc[0, "hostname"] == "ACME-WS-001"
    assert frame.loc[1, "os_build"] == 20348
    assert frame.loc[0, "vendor"] == "crowdstrike"
    assert frame.loc[1, "machine_type"] == "server"


def test_last_seen_is_tz_aware():
    frame = parse_agent_csv(FULL_CSV)
    assert isinstance(frame["last_seen"].dtype, pd.DatetimeTZDtype)
    assert str(frame["last_seen"].dtype.tz) == "UTC"


def test_minimal_csv_only_hostname_defaults_everything_else():
    frame = parse_agent_csv(MINIMAL_CSV)
    assert len(frame) == 2
    assert frame["join_key"].tolist() == ["acme-ws-001", "acme-dc01"]
    assert (frame["os"] == "").all()
    assert frame["os_build"].isna().all()
    assert frame["last_seen"].isna().all()
    assert (frame["vendor"] == "").all()
    assert (frame["platform"] == "").all()
    assert (frame["machine_type"] == "").all()


def test_rejects_csv_without_hostname_column():
    with pytest.raises(AgentCSVParseError):
        parse_agent_csv("Oops,Something\nbroke,badly\n")


def test_drops_rows_with_empty_hostname():
    csv_text = "hostname,vendor\nACME-WS-001,crowdstrike\n,crowdstrike\n"
    frame = parse_agent_csv(csv_text)
    assert frame["join_key"].tolist() == ["acme-ws-001"]


def test_os_build_blank_is_none_not_error():
    csv_text = "hostname,os_build\nACME-WS-001,\nACME-DC01,20348\n"
    frame = parse_agent_csv(csv_text)
    by_key = frame.set_index("join_key")["os_build"]
    assert by_key["acme-dc01"] == 20348
    assert pd.isna(by_key["acme-ws-001"])
