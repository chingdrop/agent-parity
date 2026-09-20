# 0006. Derive machine_type from OS text and backfill it for agentless devices

Status: Accepted (2026-07-04)

## Context

The tool exists to flag high-value assets (Domain Controllers, file/storage servers) ahead of a random workstation.
`machine_type` originally came only from the agent side of the merge, so a `missing_agent` row carried no criticality
signal, which is backwards for a coverage tool. Domain Controllers are identifiable by a distinctive OU, but file and
storage servers can be named anything.

## Decision

`infer_machine_type()` returns `"server"` if the OS text contains "server", else `"desktop"`. `backfill_machine_type`
applies it to AD's OS text for any row with no agent-reported `machine_type`. An agent-reported value always wins.
Hostnames are never inspected.

## Alternatives considered

- **Hostname-pattern heuristics**: rejected as guessing, since a storage server "can be named anything; it can't fake
  being a Windows Server".

## Consequences

- Every row, including a missing Domain Controller, gets a `machine_type`.
- `summarize()` reports `server_coverage_pct` next to `coverage_pct`, and the frame is filterable by `machine_type`.
- It is a text heuristic: OS text without "server", including blank text, yields `"desktop"`.
- Rules out naming-convention-based criticality.

## Evidence

- Code: `backfill_machine_type` in [`src/agent_parity/correlation.py`](../../src/agent_parity/correlation.py),
  `infer_machine_type` in [`src/agent_parity/models.py`](../../src/agent_parity/models.py)
- Tests: [`tests/test_correlation.py`](../../tests/test_correlation.py)
  `test_missing_agent_server_is_backfilled_as_a_server`,
  `test_backfill_never_overwrites_an_agent_reported_machine_type`,
  `test_server_coverage_pct_is_scoped_to_servers_only`; [
  `tests/connectors/test_connectors.py`](../../tests/connectors/test_connectors.py) `test_infer_machine_type`
