# Sample report — Acme Corp

> Generated from synthetic sample data (Acme Corp).

> Produced by `scripts/gen_sample_report.py` from a real `agent-parity` run against `sample_data/` on 2026-09-20. OS lifecycle is evaluated as of that date; coverage counts don't depend on it.

## Coverage at a glance

51 rows covering 48 unique devices (a device reporting to two vendors gets one row per vendor).

| Status | Rows |
|---|---|
| `covered` | 36 |
| `stale_coverage` | 3 |
| `missing_agent` | 5 |
| `orphaned_agent` | 7 |

| Metric | Value | Basis |
|---|---|---|
| `coverage_pct` | 81.8% | covered ÷ (covered + stale + missing), all devices AD knows about |
| `server_coverage_pct` | 87.5% | the same ratio, servers only |

## High-value assets: servers and Domain Controllers

Servers are pulled with the `machine_type == "server"` filter. Of 10 server rows, 7 are `covered`, 1 `missing_agent`, 0 `stale_coverage` and 2 `orphaned_agent`. That is why `server_coverage_pct` (87.5%) can differ from the overall `coverage_pct` (81.8%): a gap on a server matters more than the same gap on a laptop.

**Servers with missing or stale coverage (1):**

| Device | Type | Coverage | OS | OS lifecycle | Last check-in |
|---|---|---|---|---|---|
| `ACME-SQL02` | server | `missing_agent` | Windows Server 2022 Datacenter | `supported` | never / no agent |

**Domain Controllers:** 2 Domain Controllers in AD (`ACME-DC01`, `ACME-DC02`), and none is missing or stale — all are `covered`. Domain Controllers are identified from AD's own `OU=Domain Controllers` container, not from hostnames.

## OS end-of-life

Independent of coverage: a device on an end-of-life OS is a finding even with a healthy agent. Lifecycle dates come from the committed reference data under `src/agent_parity/` (sourced from endoflife.date).

| OS lifecycle | Rows |
|---|---|
| `end_of_life` | 32 |
| `eol_soon` | 7 |
| `supported` | 10 |
| `unknown` | 2 |

**OS lifecycle × coverage status** (rows):

| OS lifecycle | `covered` | `stale_coverage` | `missing_agent` | `orphaned_agent` | Total |
|---|---|---|---|---|---|
| `end_of_life` | 24 | 2 | 4 | 2 | 32 |
| `eol_soon` | 5 | 1 | 0 | 1 | 7 |
| `supported` | 7 | 0 | 1 | 2 | 10 |
| `unknown` | 0 | 0 | 0 | 2 | 2 |

`at_risk_status_counts` — the `end_of_life` and `eol_soon` rows (39 in total) broken down by coverage status: 29 `covered`, 3 `stale_coverage`, 4 `missing_agent`, 3 `orphaned_agent`.

### Worst case: an unsupported OS with no agent

4 devices in AD run an `end_of_life` OS and have no agent reporting at all (`missing_agent`): nothing is watching them, and the OS itself is no longer supported. None of them is a server in this dataset; all are workstations or laptops.

| Device | Type | Coverage | OS | OS lifecycle | Last check-in |
|---|---|---|---|---|---|
| `ACME-LT-009` | desktop | `missing_agent` | Windows 11 Enterprise | `end_of_life` | never / no agent |
| `ACME-WS-021` | desktop | `missing_agent` | Windows 11 Enterprise | `end_of_life` | never / no agent |
| `ACME-WS-022` | desktop | `missing_agent` | Windows 11 Enterprise | `end_of_life` | never / no agent |
| `ACME-WS-025` | desktop | `missing_agent` | Windows 10 Enterprise | `end_of_life` | never / no agent |

## How to read this

Three independent questions, each answered per device. **Coverage** says whether an agent is watching it (`covered`), is silent (`stale_coverage`: matched, but no recent check-in), was never deployed (`missing_agent`), or is reporting for a machine AD has no record of (`orphaned_agent`: decommissioned, shadow IT, or a naming mismatch). **Machine type** says how much a gap costs, so a missing server or Domain Controller outranks a missing laptop. **OS lifecycle** says whether the platform itself is still supported, which no agent can fix. Read the queue top-down: uncovered servers first, then end-of-life systems with no agent, then stale check-ins, then orphans to clean up. `coverage_pct` is the trend line for a quarterly report; `server_coverage_pct` is the number to defend.
