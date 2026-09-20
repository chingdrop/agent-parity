# 0007. Normalize cross-vendor wording to SentinelOne's, but leave agent_version alone

Status: Accepted (2026-07-04)

## Context

Analysts read "windows", "server" and "desktop" and expect that wording whichever vendor produced a row. SentinelOne's API vocabulary is what reports were already standardized on. Carbon Black and BitDefender report the same facts differently or not at all.

## Decision

`AgentDevice.platform` and `machine_type` use SentinelOne's wording. SentinelOne's values pass straight through. Carbon Black's uppercase `os` is lowercased, and its `machine_type` is inferred from OS text. BitDefender's numeric `machineType` is mapped to strings, and its `platform` is inferred from OS text. `agent_version` is never normalized: each vendor's real version string is kept.

## Alternatives considered

- **Making `agent_version` look like SentinelOne's**: rejected; it would fabricate a number rather than normalize one.
- <!-- TODO(craig): any other canonical vocabularies considered (a vendor-neutral one, for example). -->

## Consequences

- Reports read the same across vendors.
- Carbon Black's `machine_type` and BitDefender's `platform` are inferences from OS text, not vendor data.
- `agent_version` values are not comparable across vendors.
- A new vendor needs a per-field choice: map directly if the vendor reports the field, infer only if it doesn't.

## Evidence

- Code: [`src/agent_parity/connectors/carbonblack.py`](../../src/agent_parity/connectors/carbonblack.py), [`src/agent_parity/connectors/bitdefender.py`](../../src/agent_parity/connectors/bitdefender.py), [`src/agent_parity/connectors/sentinelone.py`](../../src/agent_parity/connectors/sentinelone.py), [`src/agent_parity/models.py`](../../src/agent_parity/models.py)
- Tests: [`tests/connectors/test_connectors.py`](../../tests/connectors/test_connectors.py) `test_fixture_inventory_normalizes_platform_and_machine_type_to_s1_wording`, `test_carbonblack_lowercases_its_uppercase_os_enum`, `test_bitdefender_maps_its_numeric_machine_type_enum_to_s1_wording`. No test asserts the `agent_version` pass-through.
