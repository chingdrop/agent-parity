# 0008. Classify OS end-of-life by build number, falling back to a free-text table

Status: Accepted (2026-07-04)

## Context

A free-text OS name is ambiguous past Windows 10: "Windows 11 Enterprise" doesn't say which feature update, and each has
its own end-of-life date. Active Directory exposes an exact build natively (`operatingSystemVersion`) and SentinelOne
carries one; Carbon Black and BitDefender have no equivalent field.

## Decision

`eol_status_for_device` looks up the build number first (`os_eol_builds_data.json`, agent-reported build before AD's)
and falls back to free-text matching (`os_eol_data.json`) only when no build exists. The free-text table deliberately
has no bare "Windows 11" entry, so such a device stays `unknown` rather than getting a guessed date. Both files are
hand-curated from endoflife.date.

## Alternatives considered

- **One end-of-life date for bare "Windows 11"**: rejected as "a guess dressed up as data".
- **Fetching endoflife.date live**: noted as a natural extension, not built; a static file answers the question, and
  `scripts/check_eol_drift.py` checks it on demand.

## Consequences

- Devices seen only through Carbon Black or BitDefender with no build fall back to free text, so a Windows 11 device
  there is `unknown`.
- A `missing_agent` row still gets a precise status from AD's build.
- The data is static and hand-maintained; the drift check is manual, not part of CI.
- Both files use the earliest end-of-life date across a version's editions; `eol_soon` means within 180 days of today.

## Evidence

- Code: [`src/agent_parity/os_eol.py`](../../src/agent_parity/os_eol.py), [
  `src/agent_parity/os_eol_data.json`](../../src/agent_parity/os_eol_data.json), [
  `src/agent_parity/os_eol_builds_data.json`](../../src/agent_parity/os_eol_builds_data.json), `classify_eol_status`
  in [`src/agent_parity/correlation.py`](../../src/agent_parity/correlation.py), [
  `scripts/check_eol_drift.py`](../../scripts/check_eol_drift.py)
- Tests: [`tests/test_os_eol.py`](../../tests/test_os_eol.py) `test_eol_date_for_unknown_os_returns_none`,
  `test_eol_status_for_device_prefers_build_over_free_text`,
  `test_eol_status_for_device_distinguishes_feature_updates`; [
  `tests/test_correlation.py`](../../tests/test_correlation.py)
  `test_missing_agent_row_uses_ad_build_for_precise_eol_status`
