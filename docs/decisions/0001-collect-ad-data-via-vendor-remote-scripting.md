# 0001. Collect AD data by running a script through the EDR vendor's remote scripting

Status: Accepted (2026-07-03)

## Context

The tool needs a full list of Active Directory computer objects to compare against the agent inventory. agent-parity
should not hold domain credentials. An endpoint that is already domain-joined and already managed by the EDR vendor sits
in an existing trust relationship, so it can run the query on the tool's behalf.

## Decision

Push `Export-ADDevices.ps1` to an already domain-joined, already-managed endpoint and run it through the vendor's own
remote scripting (SentinelOne Remote Script Orchestration, Carbon Black Live Response). agent-parity never binds to
LDAP. `pick_ad_export_vendor()` chooses the capable vendor for each client.

## Alternatives considered

- **Direct LDAP bind from agent-parity**: the option this design avoids, since agent-parity would have to hold domain
  credentials.
- **WinRM / PowerShell Remoting**: rejected. It would mean configuring and maintaining remote access to a domain
  controller, and making that access reachable from outside, just to generate a computer inventory. SentinelOne was
  already installed, and the export could be uploaded over HTTPS, which was already allowed and secured.

## Consequences

- Every client needs at least one enabled vendor that really supports remote execution; otherwise `ConfigError`
  (see [0004](0004-bitdefender-connector-is-inventory-only.md)).
- Each AD domain needs a domain-joined endpoint the vendor can reach
  (see [0010](0010-multi-domain-ad-one-export-per-domain.md)).
- The export comes back through object storage, not the vendor's output channel
  (see [0002](0002-return-ad-export-via-presigned-put-url.md)).
- Rules out LDAP-based collection.

## Evidence

- Code: [`src/agent_parity/script_runner.py`](../../src/agent_parity/script_runner.py), [
  `src/agent_parity/scripts/Export-ADDevices.ps1`](../../src/agent_parity/scripts/Export-ADDevices.ps1),
  `pick_ad_export_vendor` in [`src/agent_parity/config.py`](../../src/agent_parity/config.py)
- Tests: [`tests/test_config.py`](../../tests/test_config.py) `test_ad_export_prefers_sentinelone_over_carbonblack`,
  `test_ad_export_raises_when_only_bitdefender_is_enabled`; [
  `tests/connectors/test_sentinelone.py`](../../tests/connectors/test_sentinelone.py)
  `test_live_deploy_and_run_round_trips_full_rso_sequence`
