"""Tests for the CLI entrypoint (agent_parity/cli.py)."""

from datetime import UTC, datetime

import pandas as pd
from click.testing import CliRunner

from agent_parity import cli

NOW = datetime.now(UTC).isoformat()

AD_CSV = f"""\
Name,DNSHostName,OperatingSystem,LastLogonTimestamp,Enabled,DistinguishedName
CORP-WS-001,corp-ws-001.corp.example,Windows 11 Enterprise,{NOW},True,"CN=CORP-WS-001,OU=Workstations,DC=corp,DC=example"
"""

AGENT_CSV = f"""\
hostname,vendor,last_seen
CORP-WS-001,crowdstrike,{NOW}
"""


def test_compare_writes_output_and_returns_zero(tmp_path):
    ad_csv = tmp_path / "ad.csv"
    agent_csv = tmp_path / "agent.csv"
    ad_csv.write_text(AD_CSV)
    agent_csv.write_text(AGENT_CSV)
    out_path = tmp_path / "result.csv"

    result = CliRunner().invoke(cli.cli, ["compare", str(ad_csv), str(agent_csv), "--out", str(out_path)])

    assert result.exit_code == 0, result.output
    frame = pd.read_csv(out_path)
    assert len(frame) == 1
    assert frame.loc[0, "status"] == "covered"


def test_compare_defaults_output_path_to_agent_csv_stem(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")
    ad_csv = tmp_path / "ad.csv"
    agent_csv = tmp_path / "crowdstrike_export.csv"
    ad_csv.write_text(AD_CSV)
    agent_csv.write_text(AGENT_CSV)

    result = CliRunner().invoke(cli.cli, ["compare", str(ad_csv), str(agent_csv)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "output" / "crowdstrike_export_correlated.csv").exists()


def test_compare_reports_missing_file_without_a_traceback(tmp_path):
    result = CliRunner().invoke(cli.cli, ["compare", str(tmp_path / "nope.csv"), str(tmp_path / "also-nope.csv")])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_compare_reports_parse_errors_without_raising(tmp_path):
    ad_csv = tmp_path / "ad.csv"
    agent_csv = tmp_path / "agent.csv"
    ad_csv.write_text("Oops,Something\nbroke,badly\n")
    agent_csv.write_text(AGENT_CSV)

    result = CliRunner().invoke(cli.cli, ["compare", str(ad_csv), str(agent_csv), "--out", str(tmp_path / "out.csv")])

    assert result.exit_code == 1


def _snapshot_count() -> int:
    from agent_parity.scheduling.db import CoverageSnapshot, get_engine, session_factory

    Session = session_factory(get_engine())
    with Session() as session:
        return session.query(CoverageSnapshot).count()


def test_run_persists_a_run_without_writing_a_csv_by_default(tmp_path, monkeypatch, sqlite_db):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert result.exit_code == 0, result.output
    assert "[acme] run 1: complete, 51 rows (coverage" in result.output
    assert _snapshot_count() == 51
    assert not (tmp_path / "output").exists()


def test_run_with_csv_also_writes_the_classified_frame(tmp_path, monkeypatch, sqlite_db):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--csv"])

    assert result.exit_code == 0, result.output
    out_path = tmp_path / "output" / "acme.csv"
    assert f"-> {out_path}" in result.output
    assert len(pd.read_csv(out_path)) == 51
    assert _snapshot_count() == 51


def test_run_all_persists_and_writes_one_csv_per_client(tmp_path, monkeypatch, sqlite_db):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")

    result = CliRunner().invoke(cli.cli, ["run", "--all", "--csv"])

    assert result.exit_code == 0, result.output
    assert "[acme] run" in result.output
    assert "[globex] run" in result.output
    assert (tmp_path / "output" / "acme.csv").exists()
    assert (tmp_path / "output" / "globex.csv").exists()


def test_run_rejects_unknown_client(sqlite_db):
    result = CliRunner().invoke(cli.cli, ["run", "--client", "nope"])

    assert result.exit_code != 0


def test_run_exits_nonzero_when_every_ad_domain_fails(tmp_path, monkeypatch, sqlite_db):
    from agent_parity.scheduling import persistence

    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        persistence,
        "run_correlation_for_client",
        lambda config, client_cfg, stale_days=None: (None, {"ad:ACME-DC01": "error: offline"}),
    )

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--csv"])

    assert result.exit_code == 1
    assert "run 1: failed" in result.output
    assert not (tmp_path / "output" / "acme.csv").exists()
