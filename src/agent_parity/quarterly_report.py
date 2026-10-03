"""The quarterly client report: one PDF per client per quarter.

Mirrors the report the original tool's output fed every quarter: agent coverage
climbing quarter over quarter, high-value assets (servers) called out, the
itemized gaps for the client to act on, and devices on an end-of-life OS.

This module only renders. It takes plain data (``QuarterlyReport``) and knows
nothing about SQLAlchemy; ``agent_parity.scheduling.history.build_quarterly_report``
builds that data from the run history. ReportLab is an optional dependency
(``agent-parity[report]``), imported only when a PDF is actually rendered.

Counts are rows, the same unit as every other output: a device reporting to two
vendors is one row per vendor, and coverage is covered rows over the rows AD knows
about (``correlation.coverage_pct``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from agent_parity.models import CoverageStatus, OSLifecycleStatus

_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Quarter:
    year: int
    number: int

    @classmethod
    def parse(cls, text: str) -> Quarter:
        """``"2026-Q3"`` (or ``"2026Q3"``) -> ``Quarter(2026, 3)``."""
        match = _QUARTER.match(text.strip())
        if not match:
            raise ValueError(f"not a quarter: {text!r} (expected e.g. 2026-Q3)")
        return cls(int(match.group(1)), int(match.group(2)))

    @classmethod
    def of(cls, moment: date | datetime) -> Quarter:
        return cls(moment.year, (moment.month - 1) // 3 + 1)

    @property
    def start(self) -> datetime:
        return datetime(self.year, 3 * self.number - 2, 1)

    @property
    def end(self) -> datetime:
        """Exclusive: the start of the next quarter."""
        return self.next().start

    def next(self) -> Quarter:
        return Quarter(self.year + 1, 1) if self.number == 4 else Quarter(self.year, self.number + 1)

    def previous(self) -> Quarter:
        return Quarter(self.year - 1, 4) if self.number == 1 else Quarter(self.year, self.number - 1)

    def __str__(self) -> str:
        return f"{self.year}-Q{self.number}"


@dataclass(frozen=True)
class QuarterPoint:
    """One quarter of the trend: that quarter's last finished run."""

    quarter: Quarter
    run_id: int
    run_started_at: datetime
    coverage_pct: float
    server_coverage_pct: float
    status_counts: dict[str, int]
    server_status_counts: dict[str, int]


@dataclass(frozen=True)
class DeviceRow:
    hostname: str
    os: str
    status: str
    vendor: str
    agent_last_seen: datetime | None
    machine_type: str
    eol_status: str
    os_build: int | None = None

    @property
    def is_server(self) -> bool:
        return self.machine_type == "server"

    @property
    def os_with_build(self) -> str:
        """The build pins which feature update a device runs, which decides its EOL date."""
        os = self.os or "—"
        return f"{os} (build {self.os_build})" if self.os_build else os


@dataclass(frozen=True)
class QuarterlyReport:
    client_slug: str
    client_name: str
    quarter: Quarter
    #: Oldest first; ends with the report's own quarter.
    trend: list[QuarterPoint]
    #: Every row of the report quarter's last finished run.
    rows: list[DeviceRow]
    generated_on: date

    @property
    def current(self) -> QuarterPoint:
        return self.trend[-1]

    @property
    def previous(self) -> QuarterPoint | None:
        return self.trend[-2] if len(self.trend) > 1 else None

    def rows_with(self, status: str) -> list[DeviceRow]:
        """Rows with ``status``, servers first, then by hostname."""
        return sorted((r for r in self.rows if r.status == status), key=lambda r: (not r.is_server, r.hostname))

    def devices_by_eol(self) -> dict[str, list[DeviceRow]]:
        """OS lifecycle is a fact about the device, not each agent on it: one row
        per device (its most severe status), grouped by status."""
        severity = [s.value for s in (OSLifecycleStatus.END_OF_LIFE, OSLifecycleStatus.EOL_SOON)]
        severity += [OSLifecycleStatus.SUPPORTED.value, OSLifecycleStatus.UNKNOWN.value]
        worst: dict[str, DeviceRow] = {}
        for row in self.rows:
            kept = worst.get(row.hostname)
            if kept is None or severity.index(row.eol_status) < severity.index(kept.eol_status):
                worst[row.hostname] = row
        grouped: dict[str, list[DeviceRow]] = {status: [] for status in severity}
        for row in sorted(worst.values(), key=lambda r: (not r.is_server, r.hostname)):
            grouped[row.eol_status].append(row)
        return grouped

    def has_agent(self, hostname: str) -> bool:
        return any(r.hostname == hostname and r.status != CoverageStatus.MISSING_AGENT.value for r in self.rows)


class ReportDependencyError(Exception):
    """ReportLab isn't installed (the ``report`` extra)."""


# --- rendering -------------------------------------------------------------------

_STATUS_LABELS = {
    CoverageStatus.COVERED.value: "Covered",
    CoverageStatus.STALE_COVERAGE.value: "Stale",
    CoverageStatus.MISSING_AGENT.value: "Missing agent",
    CoverageStatus.ORPHANED_AGENT.value: "Orphaned agent",
}
_EOL_LABELS = {
    OSLifecycleStatus.END_OF_LIFE.value: "End of life",
    OSLifecycleStatus.EOL_SOON.value: "End of life soon",
    OSLifecycleStatus.SUPPORTED.value: "Supported",
    OSLifecycleStatus.UNKNOWN.value: "Unknown",
}


def render_pdf(report: QuarterlyReport, path: Path) -> Path:
    """Write ``report`` as a PDF to ``path`` and return the path."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise ReportDependencyError(
            "The quarterly report needs ReportLab: install the `report` extra "
            "(uv sync --extra report, or pip install 'agent-parity[report]')."
        ) from exc

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    h1, h2 = styles["Title"], styles["Heading2"]
    accent = colors.HexColor("#1f4e79")
    server_accent = colors.HexColor("#c55a11")

    def table(rows: list[list[str]], widths: list[float], numeric_from: int = 99) -> Table:
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce6f1")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fa")]),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, accent),
                    ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        return t

    def device_table(rows: list[DeviceRow], show_agent: bool) -> Table | Paragraph:
        """Gap list: one row per agent, so a device on two vendors shows once per agent."""
        if not rows:
            return Paragraph("None.", body)
        if show_agent:
            data = [["Device", "Type", "OS", "Agent", "Last check-in"]]
            data += [[r.hostname, _type(r), r.os or "—", r.vendor or "—", _fmt_seen(r)] for r in rows]
            return table(data, [1.6 * inch, 0.85 * inch, 2.15 * inch, 1.0 * inch, 1.1 * inch])
        data = [["Device", "Type", "OS", "Last check-in"]]
        data += [[r.hostname, _type(r), r.os or "—", _fmt_seen(r)] for r in rows]
        return table(data, [1.7 * inch, 0.9 * inch, 2.6 * inch, 1.5 * inch])

    def eol_table(rows: list[DeviceRow]) -> Table:
        data = [["Device", "Type", "OS", "OS lifecycle", "Agent"]]
        for r in rows:
            agent = "yes" if report.has_agent(r.hostname) else "NONE"
            data.append([r.hostname, _type(r), r.os_with_build, _EOL_LABELS[r.eol_status], agent])
        return table(data, [1.6 * inch, 0.85 * inch, 2.45 * inch, 1.15 * inch, 0.65 * inch])

    cur, prev = report.current, report.previous
    counts = cur.status_counts
    by_eol = report.devices_by_eol()
    eol_devices = by_eol[OSLifecycleStatus.END_OF_LIFE.value] + by_eol[OSLifecycleStatus.EOL_SOON.value]
    worst = [r for r in by_eol[OSLifecycleStatus.END_OF_LIFE.value] if not report.has_agent(r.hostname)]

    story: list = [
        Paragraph(f"{report.client_name}: endpoint agent coverage", h1),
        Paragraph(
            f"Quarterly report for {report.quarter} &nbsp;·&nbsp; data from run {cur.run_id} on "
            f"{cur.run_started_at:%Y-%m-%d} &nbsp;·&nbsp; generated {report.generated_on:%Y-%m-%d}",
            small,
        ),
        Spacer(1, 0.2 * inch),
    ]

    headline = [
        ["", "This quarter", "Change since last quarter"],
        ["Agent coverage", f"{cur.coverage_pct:.1f}%", _delta(cur.coverage_pct, prev.coverage_pct if prev else None)],
        [
            "Server coverage",
            f"{cur.server_coverage_pct:.1f}%",
            _delta(cur.server_coverage_pct, prev.server_coverage_pct if prev else None),
        ],
        *(
            [label, str(counts.get(status, 0)), _count_delta(cur, prev, status)]
            for label, status in (
                ("Devices missing an agent", "missing_agent"),
                ("Agents not checking in (stale)", "stale_coverage"),
                ("Agents with no AD record (orphaned)", "orphaned_agent"),
            )
        ),
        ["Devices on an end-of-life OS", str(len(by_eol[OSLifecycleStatus.END_OF_LIFE.value])), ""],
    ]
    story += [table(headline, [2.8 * inch, 1.3 * inch, 2.2 * inch], numeric_from=1), Spacer(1, 0.25 * inch)]

    # 1. Trend
    story += [Paragraph("1. Coverage trend", h2), _trend_chart(report, accent, server_accent), Spacer(1, 0.1 * inch)]
    trend_rows = [["Quarter", "Coverage", "Server coverage", "Covered", "Stale", "Missing", "Orphaned"]]
    for p in report.trend:
        c = p.status_counts
        trend_rows.append(
            [
                str(p.quarter),
                f"{p.coverage_pct:.1f}%",
                f"{p.server_coverage_pct:.1f}%",
                *(str(c.get(s, 0)) for s in ("covered", "stale_coverage", "missing_agent", "orphaned_agent")),
            ]
        )
    story += [table(trend_rows, [0.9 * inch] + [0.95 * inch] * 6, numeric_from=1), Spacer(1, 0.25 * inch)]

    # 2. High-value assets
    servers = [r for r in report.rows if r.is_server and r.status != "orphaned_agent"]
    server_gaps = [r for r in servers if r.status in ("missing_agent", "stale_coverage")]
    story += [
        Paragraph("2. High-value assets: servers", h2),
        Paragraph(
            f"Server coverage is {cur.server_coverage_pct:.1f}% ({len(servers)} server rows known to AD). Servers, "
            "including Domain Controllers, are identified by their Windows Server OS, not by hostname. "
            + ("Servers with a gap:" if server_gaps else "Every server has a current agent."),
            body,
        ),
    ]
    if server_gaps:
        gap_table = [["Server", "OS", "Gap", "Last check-in"]] + [
            [r.hostname, r.os or "—", _STATUS_LABELS[r.status], _fmt_seen(r)] for r in server_gaps
        ]
        story.append(table(gap_table, [1.7 * inch, 2.6 * inch, 1.2 * inch, 1.2 * inch]))
    story.append(Spacer(1, 0.25 * inch))

    # 3. Gaps
    story.append(Paragraph("3. Coverage gaps to act on", h2))
    for status, title, show_agent in (
        ("missing_agent", "Devices in AD with no agent", False),
        ("stale_coverage", "Agents that stopped checking in", True),
        ("orphaned_agent", "Agents with no matching AD computer", True),
    ):
        rows = report.rows_with(status)
        heading = Paragraph(f"<b>{title}</b> ({len(rows)})", body)
        story += [heading, device_table(rows, show_agent), Spacer(1, 0.12 * inch)]

    # 4. OS end-of-life
    eol_counts = {label: len(by_eol[key]) for key, label in _EOL_LABELS.items()}
    story += [
        Spacer(1, 0.13 * inch),
        Paragraph("4. Operating system end of life", h2),
        Paragraph(
            "Independent of agent coverage: an end-of-life OS no longer gets security updates, even with an agent. "
            + " · ".join(f"{label}: {n}" for label, n in eol_counts.items()),
            body,
        ),
        Paragraph(
            f"<b>Highest risk: end-of-life OS with no agent</b> ({len(worst)}): "
            + (", ".join(r.hostname for r in worst) if worst else "none."),
            body,
        ),
        Spacer(1, 0.08 * inch),
        eol_table(eol_devices) if eol_devices else Paragraph("None.", body),
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=f"{report.client_name} agent coverage, {report.quarter}",
        author="agent-parity",
    )

    def footer(canvas, doc_) -> None:  # noqa: ANN001 - ReportLab callback signature
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(0.75 * inch, 0.45 * inch, f"{report.client_name} · {report.quarter} · agent-parity")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


def _trend_chart(report: QuarterlyReport, accent, server_accent):  # noqa: ANN001, ANN202 - ReportLab types
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.units import inch

    drawing = Drawing(6.8 * inch, 2.4 * inch)
    chart = HorizontalLineChart()
    chart.x, chart.y = 40, 30
    chart.width, chart.height = 6.8 * inch - 170, 2.4 * inch - 50
    chart.data = [
        [p.coverage_pct for p in report.trend],
        [p.server_coverage_pct for p in report.trend],
    ]
    chart.categoryAxis.categoryNames = [str(p.quarter) for p in report.trend]
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 8
    chart.valueAxis.valueMin, chart.valueAxis.valueMax, chart.valueAxis.valueStep = 0, 100, 20
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 8
    chart.valueAxis.labelTextFormat = "%d%%"
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = accent.clone(alpha=0.15)
    for index, color in enumerate((accent, server_accent)):
        chart.lines[index].strokeColor = color
        chart.lines[index].strokeWidth = 2
    # Label each point with its value.
    chart.lineLabelFormat = "%.1f%%"
    chart.lineLabels.fontName = "Helvetica"
    chart.lineLabels.fontSize = 7
    chart.lineLabels.dy = 6
    drawing.add(chart)

    legend = Legend()
    legend.x, legend.y = chart.x + chart.width + 15, chart.y + chart.height - 10
    legend.fontName = "Helvetica"
    legend.fontSize = 8
    legend.colorNamePairs = [(accent, "All devices"), (server_accent, "Servers")]
    drawing.add(legend)
    drawing.add(String(0, chart.y + chart.height + 8, "Agent coverage by quarter", fontName="Helvetica", fontSize=8))
    return drawing


def _delta(current: float, previous: float | None) -> str:
    if previous is None:
        return "—"
    change = current - previous
    return f"{change:+.1f} points" if change else "no change"


def _count_delta(current: QuarterPoint, previous: QuarterPoint | None, status: str) -> str:
    if previous is None:
        return "—"
    change = current.status_counts.get(status, 0) - previous.status_counts.get(status, 0)
    return f"{change:+d}" if change else "no change"


def _type(row: DeviceRow) -> str:
    return "Server" if row.is_server else "Workstation"


def _fmt_seen(row: DeviceRow) -> str:
    return f"{row.agent_last_seen:%Y-%m-%d}" if row.agent_last_seen else "never / no agent"
