"""Tests for agent_parity/quarterly_report.py: quarters, the report data's
per-device views, and rendering a real PDF."""

from datetime import date, datetime

import pytest

from agent_parity.quarterly_report import (
    DeviceRow,
    Quarter,
    QuarterlyReport,
    QuarterPoint,
    ReportDependencyError,
    render_pdf,
)

# --- Quarter ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["2026-Q3", "2026q3", " 2026-Q3 "])
def test_quarter_parses_its_usual_spellings(text):
    assert Quarter.parse(text) == Quarter(2026, 3)


@pytest.mark.parametrize("text", ["2026-Q5", "Q3-2026", "2026"])
def test_quarter_rejects_anything_else(text):
    with pytest.raises(ValueError, match="not a quarter"):
        Quarter.parse(text)


def test_quarter_bounds_and_neighbours():
    q4 = Quarter(2026, 4)

    assert q4.start == datetime(2026, 10, 1)
    assert q4.end == datetime(2027, 1, 1)
    assert q4.next() == Quarter(2027, 1)
    assert Quarter(2027, 1).previous() == q4
    assert Quarter.of(date(2026, 9, 30)) == Quarter(2026, 3)
    assert str(q4) == "2026-Q4"


# --- report data -------------------------------------------------------------------


def row(hostname, status="covered", eol="supported", machine_type="desktop", vendor="sentinelone", build=None):
    return DeviceRow(
        hostname=hostname,
        os="Windows 11 Enterprise",
        status=status,
        vendor=vendor,
        agent_last_seen=datetime(2026, 9, 1),
        machine_type=machine_type,
        eol_status=eol,
        os_build=build,
    )


def point(quarter, coverage, server_coverage=90.0, **counts):
    return QuarterPoint(
        quarter=quarter,
        run_id=1,
        run_started_at=quarter.start,
        coverage_pct=coverage,
        server_coverage_pct=server_coverage,
        status_counts=counts,
        server_status_counts={},
    )


def report(rows, trend=None):
    return QuarterlyReport(
        client_slug="acme",
        client_name="Acme Corp",
        quarter=Quarter(2026, 3),
        trend=trend or [point(Quarter(2026, 2), 60.0), point(Quarter(2026, 3), 80.0)],
        rows=rows,
        generated_on=date(2026, 10, 2),
    )


def test_gap_lists_put_servers_first():
    r = report(
        [
            row("WS-2", "missing_agent"),
            row("SRV-9", "missing_agent", machine_type="server"),
            row("WS-1", "missing_agent"),
        ]
    )

    assert [d.hostname for d in r.rows_with("missing_agent")] == ["SRV-9", "WS-1", "WS-2"]


def test_eol_is_per_device_taking_its_most_severe_status():
    """A device reporting to two vendors is two rows but one OS."""
    r = report(
        [
            row("WS-1", eol="eol_soon", vendor="sentinelone"),
            row("WS-1", eol="end_of_life", vendor="carbonblack"),
            row("WS-2", eol="end_of_life"),
            row("WS-3", eol="supported"),
        ]
    )

    by_eol = r.devices_by_eol()
    assert [d.hostname for d in by_eol["end_of_life"]] == ["WS-1", "WS-2"]
    assert by_eol["eol_soon"] == []
    assert [d.hostname for d in by_eol["supported"]] == ["WS-3"]


def test_has_agent_is_false_only_when_every_row_is_missing_agent():
    r = report([row("WS-1", "missing_agent"), row("WS-2", "stale_coverage"), row("WS-3", "covered")])

    assert not r.has_agent("WS-1")
    assert r.has_agent("WS-2")
    assert r.has_agent("WS-3")


def test_os_with_build_shows_the_build_when_known():
    assert row("WS-1", build=22631).os_with_build == "Windows 11 Enterprise (build 22631)"
    assert row("WS-1").os_with_build == "Windows 11 Enterprise"


# --- rendering ---------------------------------------------------------------------


def test_render_pdf_writes_a_pdf(tmp_path):
    rows = [
        row("SRV-1", "missing_agent", eol="end_of_life", machine_type="server"),
        row("WS-1", "stale_coverage"),
        row("ORPHAN-1", "orphaned_agent"),
        row("WS-2", eol="eol_soon", build=26100),
    ]
    path = render_pdf(report(rows), tmp_path / "nested" / "acme-2026-Q3.pdf")

    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) > 2000


def test_render_pdf_handles_a_single_quarter_and_no_gaps(tmp_path):
    only = report([row("WS-1")], trend=[point(Quarter(2026, 3), 100.0)])

    assert render_pdf(only, tmp_path / "acme.pdf").read_bytes().startswith(b"%PDF")


def test_render_pdf_explains_how_to_install_reportlab_when_missing(tmp_path, no_reportlab):

    with pytest.raises(ReportDependencyError, match="install the `report` extra"):
        render_pdf(report([row("WS-1")]), tmp_path / "acme.pdf")
