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


def test_run_subcommand_dispatches_to_config_driven_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "output" / "acme.csv").exists()


def test_run_subcommand_all_writes_one_csv_per_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")

    result = CliRunner().invoke(cli.cli, ["run", "--all"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "output" / "acme.csv").exists()
    assert (tmp_path / "output" / "globex.csv").exists()


def test_run_subcommand_rejects_unknown_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")

    result = CliRunner().invoke(cli.cli, ["run", "--client", "nope"])

    assert result.exit_code != 0


def test_sync_subcommand_persists_a_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PARITY_DB_URL", f"sqlite:///{tmp_path / 'test.db'}")

    result = CliRunner().invoke(cli.cli, ["sync", "--client", "acme"])

    assert result.exit_code == 0, result.output
    assert "run 1: complete" in result.output

    from agent_parity.db import CoverageSnapshot, get_engine, session_factory

    Session = session_factory(get_engine())
    with Session() as session:
        assert session.query(CoverageSnapshot).count() == 51


def test_sync_subcommand_all_persists_one_run_per_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PARITY_DB_URL", f"sqlite:///{tmp_path / 'test.db'}")

    result = CliRunner().invoke(cli.cli, ["sync", "--all"])

    assert result.exit_code == 0, result.output
    assert "[acme] run" in result.output
    assert "[globex] run" in result.output
