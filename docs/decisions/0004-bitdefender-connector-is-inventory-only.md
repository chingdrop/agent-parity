# 0004. BitDefender connector is inventory-only

Status: Accepted (2026-07-03)

## Context

Per the connector's own notes, GravityZone's remote-task API is limited to predefined task types (scan,
isolate/deisolate, install/uninstall, patch management). It has nothing equivalent to SentinelOne's Remote Script
Orchestration or Carbon Black's Live Response for pushing and running an arbitrary script
(see [0001](0001-collect-ad-data-via-vendor-remote-scripting.md)). An earlier version modeled a `createCustomScriptTask`
RPC method to fill that gap, but that method is not in GravityZone's public API.

## Decision

`BitDefenderConnector.supports_remote_execution = False`. `deploy_and_run()` raises `ConnectorError` before the
live/fixture fork, so it refuses in both modes. The invented `createCustomScriptTask` method was removed.

## Alternatives considered

- **Keep the modeled custom-script RPC**: rejected. It was an invented extrapolation, and leaving it in place implied an
  accuracy it didn't have.

## Consequences

- BitDefender can still supply agent inventory (`fetch_inventory`).
- An organization on BitDefender alone can't have its AD export collected; `pick_ad_export_vendor` raises `ConfigError`
  rather than skipping it silently.
- Any future vendor that can't run scripts should set the same flag, not leave `_live_deploy_and_run` unimplemented.
- The same module's docstring flags its `company_id` scoping filter as unverified against real GravityZone docs.

## Evidence

- Code: [`src/agent_parity/connectors/bitdefender.py`](../../src/agent_parity/connectors/bitdefender.py),
  `pick_ad_export_vendor` in [`src/agent_parity/config.py`](../../src/agent_parity/config.py)
- Tests: [`tests/connectors/test_connectors.py`](../../tests/connectors/test_connectors.py)
  `test_bitdefender_does_not_support_remote_execution`; [`tests/test_config.py`](../../tests/test_config.py)
  `test_ad_export_raises_when_only_bitdefender_is_enabled`; [
  `tests/connectors/test_base.py`](../../tests/connectors/test_base.py)
  `test_deploy_and_run_refuses_when_remote_execution_unsupported`
