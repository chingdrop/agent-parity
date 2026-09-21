# 0010. Multi-domain AD: one export per domain, concatenated, tolerant of partial failure

Status: Accepted (2026-07-04)

## Context

A client can span more than one AD domain or forest, and no single domain controller can enumerate computer objects
outside its own domain. The 2026-07-05 simplification kept this while dropping other topology, "since a single org can
genuinely span more than one AD domain/forest".

## Decision

`ClientConfig.ad_target_devices` is a tuple, and the export script runs once per entry. `collect_ad_frame` parses each
result and concatenates them with `concat_ad_frames` into one master frame before correlation. A single-domain client is
the one-element case of the same loop, not a separate path. One domain failing doesn't stop the others; the frame is
`None` (and `run_correlation_for_client` returns `None`) only when every domain fails.

## Alternatives considered

- **Finding the target by tag in the vendor console** (the original deployment): each domain's domain controller was
  tagged "runbox" in SentinelOne, and RSO deployed the script to that tag. This rebuild names the targets explicitly in
  `ad_target_devices` instead.

## Consequences

- Per-domain outcomes appear in `vendor_status` as `ad:<target_device>`.
- Domains are assumed to be disjoint namespaces; a duplicate join key across domains is not detected or deduplicated,
  matching [0005](0005-correlate-on-normalized-hostname-only.md).
- Each domain needs its own reachable domain-joined endpoint
  (see [0001](0001-collect-ad-data-via-vendor-remote-scripting.md)).
- The demo's `globex` client has two domains; `acme` has one.

## Evidence

- Code: `collect_ad_frame` in [`src/agent_parity/pipeline.py`](../../src/agent_parity/pipeline.py), `concat_ad_frames`
  in [`src/agent_parity/ad_export.py`](../../src/agent_parity/ad_export.py), [
  `sample_data/globex/`](../../sample_data/globex/)
- Tests: [`tests/test_pipeline.py`](../../tests/test_pipeline.py)
  `test_collect_ad_frame_concatenates_globexs_two_domains`, `test_collect_ad_frame_tolerates_one_domain_failing`,
  `test_collect_ad_frame_returns_none_when_every_domain_fails`; [
  `tests/test_ad_export.py`](../../tests/test_ad_export.py) `test_concat_ad_frames_combines_rows_from_every_domain`
