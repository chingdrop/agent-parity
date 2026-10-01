"""Tests for the CLI entrypoint (agent_parity/cli.py)."""

from datetime import UTC, datetime

import pandas as pd
import pytest
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


def _run_status(run_id: int) -> str:
    from agent_parity.scheduling.db import CorrelationRun, get_engine, session_factory

    with session_factory(get_engine())() as session:
        return session.get_one(CorrelationRun, run_id).status


def test_run_exits_nonzero_when_every_ad_domain_fails(tmp_path, monkeypatch, sqlite_db):
    from agent_parity import pipeline

    def offline(config, slug, target_device):
        raise ConnectionError("target endpoint offline")

    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(pipeline, "collect_ad_csv", offline)

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--csv"])

    assert result.exit_code == 1
    assert "run 1: failed: every AD domain export failed" in result.output
    assert "ad:ACME-DC01=error: target endpoint offline" in result.output
    assert not (tmp_path / "output" / "acme.csv").exists()


def test_run_marks_the_run_failed_when_the_chord_callback_raises(monkeypatch, sqlite_db):
    """In-process runs capture a task exception the way a real worker does,
    so the chord's link_error backstop marks the run FAILED instead of
    leaving it PENDING."""
    from agent_parity.scheduling import tasks

    def blow_up(*args, **kwargs):
        raise RuntimeError("correlation blew up")

    monkeypatch.setattr(tasks, "finalize_run", blow_up)

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert result.exit_code == 1
    assert "[acme] run 1: failed: correlation blew up" in result.output
    assert _run_status(1) == "failed"


def test_run_leaves_celery_out_of_eager_mode_afterwards(sqlite_db):
    from agent_parity.scheduling.celery_app import app

    CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert app.conf.task_always_eager is False


def test_run_with_workers_fails_fast_when_none_respond(monkeypatch, sqlite_db):
    monkeypatch.setattr(cli.celery_app.control, "ping", lambda timeout: [])

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--workers"])

    assert result.exit_code != 0
    assert "No Celery workers responded" in result.output
    assert "run 1" not in result.output


def test_run_with_workers_reports_an_unreachable_broker(monkeypatch, sqlite_db):
    def unreachable(timeout):
        raise OSError("Connection refused")

    monkeypatch.setattr(cli.celery_app.control, "ping", unreachable)

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--workers"])

    assert result.exit_code != 0
    assert "Can't reach the Celery broker" in result.output


def test_run_with_workers_dispatches_the_chord_instead_of_running_it_in_process(
    tmp_path, monkeypatch, sqlite_db, celery_eager
):
    """--workers skips run_eagerly; celery_eager stands in for the worker here."""
    monkeypatch.setattr(cli, "OUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cli.celery_app.control, "ping", lambda timeout: [{"worker@host": {"ok": "pong"}}])
    monkeypatch.setattr(cli, "run_eagerly", lambda: pytest.fail("--workers must not run tasks in-process"))

    result = CliRunner().invoke(cli.cli, ["run", "--all", "--csv", "--workers"])

    assert result.exit_code == 0, result.output
    assert "[acme] run 1: complete, 51 rows" in result.output
    assert "[globex] run 2: complete" in result.output
    assert len(pd.read_csv(tmp_path / "output" / "acme.csv")) == 51


def test_run_first_marks_runs_abandoned_past_the_timeout_as_failed(sqlite_db):
    from datetime import UTC, datetime, timedelta

    from agent_parity.config import load_config
    from agent_parity.scheduling.db import CorrelationRun, get_engine, init_db, session_factory
    from agent_parity.scheduling.persistence import sync_client_from_config

    engine = get_engine()
    init_db(engine)
    with session_factory(engine)() as session:
        client = sync_client_from_config(session, load_config().client("acme"))
        session.add(CorrelationRun(client_id=client.id, started_at=datetime.now(UTC) - timedelta(hours=48)))
        session.commit()
    engine.dispose()

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert result.exit_code == 0, result.output
    assert "Marked 1 run(s) still pending after 24 h as failed: 1" in result.output
    assert _run_status(1) == "failed"
    assert "[acme] run 2: complete" in result.output


def test_timeout_without_workers_is_a_usage_error(sqlite_db):
    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--timeout", "5"])

    assert result.exit_code == 2
    assert "--timeout only applies with --workers" in result.output


def test_run_with_workers_stops_waiting_at_the_timeout(monkeypatch, sqlite_db):
    """A run still going at the deadline is reported and left alone — the
    workers may yet finish it, and the abandoned-run cleanup catches it if not."""
    from celery.exceptions import TimeoutError as CeleryTimeoutError

    waits = []

    class StillRunning:
        def get(self, timeout=None):
            waits.append(timeout)
            raise CeleryTimeoutError("The operation timed out.")

    monkeypatch.setattr(cli.celery_app.control, "ping", lambda timeout: [{"worker@host": {"ok": "pong"}}])
    monkeypatch.setattr(cli, "start_client_run", lambda config, client_cfg, include_csv: (7, StillRunning()))

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme", "--workers", "--timeout", "0.5"])

    assert result.exit_code == 1
    assert "[acme] run 7: still running after 0.5 min; left running on the workers." in result.output
    assert 0 < waits[0] <= 30


def test_run_summary_notes_hostnames_ambiguous_across_ad_domains(capsys):
    report = {
        "run_id": 3,
        "status": "complete",
        "vendor_status": {"ad:CORP-DC01": "ok"},
        "rows": 10,
        "coverage_pct": 90.0,
        "status_counts": {"covered": 9, "missing_agent": 1},
        "ambiguous_join_keys": 2,
    }

    assert cli._echo_report("acme", report, write_csv=False)

    assert "; 2 hostname(s) ambiguous across AD domains)" in capsys.readouterr().out


def test_run_refreshes_os_eol_data_first_and_reports_an_update(monkeypatch, sqlite_db):
    from agent_parity import os_eol, os_eol_live

    data = os_eol.load_bundled_data()
    data["free_text"][0] = {**data["free_text"][0], "eol_date": "2099-01-01"}
    monkeypatch.setattr(os_eol_live, "fetch_lifecycle_data", lambda timeout: data)

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert result.exit_code == 0, result.output
    assert "OS end-of-life data updated from endoflife.date (1 changes)." in result.output
    assert os_eol.cache_path().exists()


def test_run_skips_the_os_eol_refresh_when_disabled(monkeypatch, sqlite_db):
    from dataclasses import replace

    from agent_parity import os_eol, os_eol_live
    from agent_parity.config import load_config

    config = replace(load_config(), refresh_os_eol=False)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(os_eol_live, "fetch_lifecycle_data", lambda timeout: pytest.fail("must not fetch"))

    result = CliRunner().invoke(cli.cli, ["run", "--client", "acme"])

    assert result.exit_code == 0, result.output
    assert not os_eol.cache_path().exists()
