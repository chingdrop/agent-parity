"""Regenerate ``docs/sample-report.md`` from the ``sample_data/`` fixtures.

Every number and table in that document comes from a real run of the
correlation pipeline (``run_correlation_for_client``, the same call
``agent-parity run`` makes) — nothing is typed by hand, so the doc can't drift
from the code. Re-run it after changing the correlation engine or the
fixtures:

    uv run python scripts/gen_sample_report.py

The OS end-of-life view is evaluated against today's date (that's how the
engine itself works), so the EOL counts can legitimately change as time
passes; the report states the date it was generated. Coverage counts don't
depend on the date — fixture timestamps are rebased at load.

Refuses to run if any of the client's connectors has live credentials
configured: this document is published, so it must only ever contain
synthetic fixture data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from agent_parity.config import get_connectors, load_config
from agent_parity.correlation import CorrelationResult
from agent_parity.pipeline import run_correlation_for_client

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "sample-report.md"

COVERAGE_ORDER = ["covered", "stale_coverage", "missing_agent", "orphaned_agent"]
EOL_ORDER = ["end_of_life", "eol_soon", "supported", "unknown"]


def _table(headers: list[str], rows: list[list[object]]) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|")

    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else plural or singular + 's'}"


def _device_rows(frame: pd.DataFrame, now: pd.Timestamp) -> list[list[object]]:
    """One display row per frame row: hostname, type, status, OS, EOL, last check-in."""
    rows = []
    for _, r in frame.iterrows():
        host = r["hostname_ad"] if pd.notna(r["hostname_ad"]) else r["hostname_agent"]
        os_text = r["os_ad"] if pd.notna(r["os_ad"]) else r["os_agent"]
        seen = (
            "never / no agent"
            if pd.isna(r["last_seen"])
            else f"{round((now - r['last_seen']).total_seconds() / 86400)} days ago"
        )
        rows.append(
            [
                f"`{host}`",
                r["machine_type"] or "—",
                f"`{r['status']}`",
                os_text if pd.notna(os_text) else "—",
                f"`{r['eol_status']}`",
                seen,
            ]
        )
    return rows


DEVICE_HEADERS = ["Device", "Type", "Coverage", "OS", "OS lifecycle", "Last check-in"]


def _assert_fixture_mode(config, slug: str) -> None:
    live = [
        f"{vendor}:{i}"
        for vendor in config.client(slug).vendors
        for i, connector in enumerate(get_connectors(config, slug, vendor))
        if connector.is_live
    ]
    if live:
        sys.exit(
            f"Refusing to generate a published report with live connectors configured ({', '.join(live)}). "
            "Unset their credentials so the run uses sample_data/ fixtures."
        )


def build_report(result: CorrelationResult, client_name: str, *, generated: datetime) -> str:
    frame, summary = result.frame, result.summary
    now = pd.Timestamp(generated)
    counts = summary["status_counts"]
    server_counts = summary["server_status_counts"]
    eol_counts = summary["eol_status_counts"]

    out: list[str] = []
    out.append(f"# Sample report — {client_name}\n")
    out.append(f"> Generated from synthetic sample data ({client_name}).\n")
    out.append(
        f"> Produced by `scripts/gen_sample_report.py` from a real `agent-parity` run against `sample_data/` "
        f"on {generated.date().isoformat()}. OS lifecycle is evaluated as of that date; "
        "coverage counts don't depend on it.\n"
    )

    # --- coverage summary --------------------------------------------------
    out.append("## Coverage at a glance\n")
    out.append(
        f"{summary['total_rows']} rows covering {summary['unique_devices']} unique devices "
        "(a device reporting to two vendors gets one row per vendor).\n"
    )
    out.append(_table(["Status", "Rows"], [[f"`{s}`", counts.get(s, 0)] for s in COVERAGE_ORDER]) + "\n")
    out.append(
        _table(
            ["Metric", "Value", "Basis"],
            [
                [
                    "`coverage_pct`",
                    f"{summary['coverage_pct']}%",
                    "covered ÷ (covered + stale + missing), all devices AD knows about",
                ],
                [
                    "`server_coverage_pct`",
                    f"{summary['server_coverage_pct']}%",
                    "the same ratio, servers only",
                ],
            ],
        )
        + "\n"
    )

    # --- high-value assets -------------------------------------------------
    servers = frame[frame["machine_type"] == "server"]
    gaps = servers[servers["status"].isin(["missing_agent", "stale_coverage"])]
    dcs = frame[frame["distinguished_name"].fillna("").str.contains("OU=Domain Controllers", case=False)]
    dc_gaps = dcs[dcs["status"].isin(["missing_agent", "stale_coverage"])]

    out.append("## High-value assets: servers and Domain Controllers\n")
    out.append(
        f'Servers are pulled with the `machine_type == "server"` filter. Of {_plural(len(servers), "server row")}, '
        f"{server_counts.get('covered', 0)} are `covered`, {server_counts.get('missing_agent', 0)} `missing_agent`, "
        f"{server_counts.get('stale_coverage', 0)} `stale_coverage` and {server_counts.get('orphaned_agent', 0)} "
        f"`orphaned_agent`. That is why `server_coverage_pct` ({summary['server_coverage_pct']}%) can differ from the "
        f"overall `coverage_pct` ({summary['coverage_pct']}%): a gap on a server matters more than the same gap "
        "on a laptop.\n"
    )
    if len(gaps):
        out.append(f"**Servers with missing or stale coverage ({len(gaps)}):**\n")
        out.append(_table(DEVICE_HEADERS, _device_rows(gaps, now)) + "\n")
    else:
        out.append("No server is missing or stale.\n")
    dc_names = sorted({str(h) for h in dcs["hostname_ad"].dropna()})
    if len(dcs) == 0:
        out.append("No Domain Controllers were found in the AD export.\n")
    elif len(dc_gaps) == 0:
        out.append(
            f"**Domain Controllers:** {_plural(len(dc_names), 'Domain Controller')} in AD "
            f"({', '.join(f'`{n}`' for n in dc_names)}), and none is missing or stale — all are `covered`. "
            "Domain Controllers are identified from AD's own `OU=Domain Controllers` container, not from hostnames.\n"
        )
    else:
        out.append(f"**Domain Controllers with missing or stale coverage ({len(dc_gaps)}):**\n")
        out.append(_table(DEVICE_HEADERS, _device_rows(dc_gaps, now)) + "\n")

    # --- OS end-of-life ----------------------------------------------------
    out.append("## OS end-of-life\n")
    out.append(
        "Independent of coverage: a device on an end-of-life OS is a finding even with a healthy agent. "
        "Lifecycle dates come from the committed reference data under `src/agent_parity/` "
        "(sourced from endoflife.date).\n"
    )
    out.append(_table(["OS lifecycle", "Rows"], [[f"`{s}`", eol_counts.get(s, 0)] for s in EOL_ORDER]) + "\n")

    out.append("**OS lifecycle × coverage status** (rows):\n")
    cross = pd.crosstab(frame["eol_status"], frame["status"])
    cols = [c for c in COVERAGE_ORDER if c in cross.columns]
    out.append(
        _table(
            ["OS lifecycle", *[f"`{c}`" for c in cols], "Total"],
            [
                [
                    f"`{s}`",
                    *[int(cross.loc[s, c]) if s in cross.index else 0 for c in cols],
                    int(cross.loc[s].sum()) if s in cross.index else 0,
                ]
                for s in EOL_ORDER
            ],
        )
        + "\n"
    )
    at_risk = summary["at_risk_status_counts"]
    at_risk_total = sum(at_risk.values())
    out.append(
        f"`at_risk_status_counts` — the `end_of_life` and `eol_soon` rows ({at_risk_total} in total) broken down by "
        f"coverage status: "
        + ", ".join(f"{at_risk.get(s, 0)} `{s}`" for s in COVERAGE_ORDER if at_risk.get(s, 0))
        + ".\n"
    )

    worst = frame[(frame["eol_status"] == "end_of_life") & (frame["status"] == "missing_agent")]
    worst_servers = worst[worst["machine_type"] == "server"]
    out.append("### Worst case: an unsupported OS with no agent\n")
    if len(worst):
        out.append(
            f"{_plural(len(worst), 'device')} in AD run an `end_of_life` OS and have no agent reporting at all "
            f"(`missing_agent`): nothing is watching them, and the OS itself is no longer supported. "
            + (
                f"{_plural(len(worst_servers), 'of these is a server', 'of these are servers')}.\n"
                if len(worst_servers)
                else "None of them is a server in this dataset; all are workstations or laptops.\n"
            )
        )
        out.append(
            _table(
                DEVICE_HEADERS,
                _device_rows(worst.sort_values(["machine_type", "hostname_ad"], ascending=[False, True]), now),
            )
            + "\n"
        )
    else:
        out.append("No device combines an end-of-life OS with a missing agent in this dataset.\n")

    # --- how to read -------------------------------------------------------
    out.append("## How to read this\n")
    out.append(
        "Three independent questions, each answered per device. **Coverage** says whether an agent is watching it "
        "(`covered`), is silent (`stale_coverage`: matched, but no recent check-in), was never deployed "
        "(`missing_agent`), or is reporting for a machine AD has no record of (`orphaned_agent`: decommissioned, "
        "shadow IT, or a naming mismatch). **Machine type** says how much a gap costs, so a missing server or Domain "
        "Controller outranks a missing laptop. **OS lifecycle** says whether the platform itself is still supported, "
        "which no agent can fix. Read the queue top-down: uncovered servers first, then end-of-life systems with no "
        "agent, then stale check-ins, then orphans to clean up. `coverage_pct` is the trend line for a quarterly "
        "report; `server_coverage_pct` is the number to defend.\n"
    )
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--client", default="acme", help="client slug from config.yaml (default: acme)")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"output path (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})"
    )
    args = parser.parse_args()

    config = load_config()
    if args.client not in config.clients:
        sys.exit(f"Unknown client {args.client!r}; configured: {', '.join(sorted(config.clients))}")
    _assert_fixture_mode(config, args.client)

    client_cfg = config.client(args.client)
    result, vendor_status = run_correlation_for_client(config, client_cfg)
    if result is None:
        sys.exit(f"Every AD export failed for {args.client!r}: {vendor_status}")

    report = build_report(result, client_cfg.name, generated=datetime.now(UTC))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(f"wrote {args.out.relative_to(REPO_ROOT) if args.out.is_relative_to(REPO_ROOT) else args.out}")


if __name__ == "__main__":
    main()
