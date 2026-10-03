# 0008. Classify OS end-of-life by build number, falling back to a free-text table

Status: Accepted (2026-07-04). Amended 2026-10-01: builds are keyed by product, and servers resolve by
release name first (see Consequences).

## Context

A free-text OS name is ambiguous past Windows 10: "Windows 11 Enterprise" doesn't say which feature update, and each has
its own end-of-life date. Active Directory exposes an exact build natively (`operatingSystemVersion`) and SentinelOne
carries one; Carbon Black and BitDefender have no equivalent field.

## Decision

`eol_status_for_device` looks up the build number first (agent-reported build before AD's) and falls back to
free-text matching only when no build exists. The free-text table deliberately has no bare "Windows 11" entry, so such
a device stays `unknown` rather than getting a guessed date. Both tables live in `os_eol_data.json`, derived from
endoflife.date by `agent_parity.os_eol_live`.

## Alternatives considered

- **One end-of-life date for bare "Windows 11"**: rejected as "a guess dressed up as data".
- **Fetching endoflife.date live**: built on 2026-10-01, matching the original tool, which queried it every run.
  `os_eol_live.refresh_cache` refreshes a cache daily (beat) and at `run` start when it's over a day old; the bundled
  snapshot is the fallback.

## Consequences

- Devices seen only through Carbon Black or BitDefender with no build fall back to free text, so a Windows 11 device
  there is `unknown`.
- A `missing_agent` row still gets a precise status from AD's build.
- The data is a snapshot regenerated with `scripts/check_eol_drift.py --write` rather than edited by hand.
- Builds use the earliest end-of-life date across a version's editions; `eol_soon` means within 180 days of today.
- **Amendment (2026-10-01): build numbers are shared across products.** 26100 is Windows 11 24H2 and Windows Server
  2025; 17763 is Server 2019 and a semi-annual server release. A build-only lookup flagged every Server 2025 machine end
  of life from 2026-10-14. Builds are now keyed by product, and a server is resolved by its named release first ("Windows
  Server 2019" is exact), using the server build table only when its name has no year. Clients still go build-first.

## Evidence

- Code: [`src/agent_parity/os_eol.py`](../../src/agent_parity/os_eol.py), [
  `src/agent_parity/os_eol_data.json`](../../src/agent_parity/os_eol_data.json), [
  `src/agent_parity/os_eol_live.py`](../../src/agent_parity/os_eol_live.py), `classify_eol_status`
  in [`src/agent_parity/correlation.py`](../../src/agent_parity/correlation.py), [
  `scripts/check_eol_drift.py`](../../tools/check_eol_drift.py)
- Tests: [`tests/test_os_eol.py`](../../tests/test_os_eol.py) `test_eol_date_for_unknown_os_returns_none`,
  `test_eol_status_for_device_prefers_build_over_free_text`,
  `test_eol_status_for_device_distinguishes_feature_updates`,
  `test_windows_server_2025_is_not_given_windows_11_24h2s_date`; [
  `tests/test_correlation.py`](../../tests/test_correlation.py)
  `test_missing_agent_row_uses_ad_build_for_precise_eol_status`
