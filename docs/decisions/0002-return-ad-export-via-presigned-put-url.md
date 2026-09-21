# 0002. Return the AD export through object storage with a presigned PUT URL

Status: Accepted (2026-07-03)

## Context

Vendor remote-execution output channels (SentinelOne RSO's fetch-files, Carbon Black Live Response's command output)
don't reliably preserve exact formatting (encoding, line endings) and have output-size limits a large environment's
export can exceed. The remote endpoint should also never hold a standing storage credential.

## Decision

agent-parity generates a single-object presigned PUT URL (default `expires_in=900`, 15 minutes) and passes it to the
script as `UploadUrl`. The script uploads its CSV directly. agent-parity downloads it with its own credentials and
deletes it, best-effort. The vendor call's return value is discarded. Object storage is required for any live export;
fixture mode bypasses it.

## Alternatives considered

- **The vendor's stdout / output channel**: rejected for the formatting and size reasons above.
- <!-- TODO(craig): any other handoff options you considered (for example a network share or an inbound endpoint) and why they lost. -->

## Consequences

- A live connector with no storage configured raises `ScriptExecutionError`; there is no fallback to the vendor channel.
- Cleanup is best-effort and never fails an export that already succeeded.
- The demo path leaves storage unconfigured, which is safe only because it has no live credentials either.
- Rules out returning the CSV inline through the vendor channel.

## Evidence

- Code: [`src/agent_parity/script_runner.py`](../../src/agent_parity/script_runner.py),
  `presigned_put_url` in [`src/agent_parity/storage.py`](../../src/agent_parity/storage.py)
- Tests: [`tests/test_script_runner.py`](../../tests/test_script_runner.py)
  `test_live_connector_without_storage_raises_clear_error`,
  `test_fixture_mode_never_touches_storage_even_if_configured`, `test_live_mode_with_storage_uploads_then_downloads`; [
  `tests/test_storage.py`](../../tests/test_storage.py) `test_presigned_put_url_round_trips_content`,
  `test_presigned_url_expires_quickly_by_default` (passes `expires_in=900` explicitly; the default itself is set only in
  the function signature)
