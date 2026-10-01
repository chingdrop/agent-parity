# 0005. Correlate on normalized hostname only; the merge indicator is the classification

Status: Accepted (2026-07-03)

## Context

The AD export and the agent inventories must be joined, and the same machine can appear as
`ACME-WS-014.corp.acme.example` in one and `acme-ws-014` in the other.

## Decision

The join key is the hostname with any DNS suffix stripped, lowercased and trimmed. The two sides are outer-merged with
`indicator=True`, and the indicator is the classification: `left_only` is `missing_agent`, `right_only` is
`orphaned_agent`, and `both` is `covered` or `stale_coverage` depending on a `last_seen` cutoff (14 days by default).
Matched rows carry `match_method = "hostname_exact"`. There is no fuzzy matching.

## Alternatives considered

- **Fuzzy hostname matching**: not built, on purpose. Exact matching keeps every result traceable to an identical key. A
  renamed machine shows as a missing agent plus an orphan until the agent reports its new hostname, then resolves
  itself. Fuzzy matching would fix that short window at the cost of permanent, silent errors: a wrong match marks a
  device covered when it isn't, and sequential hostnames (`acme-ws-001`, `acme-ws-002`) are one character apart.

## Consequences

- FQDN and case differences resolve; a renamed machine does not, until its agent reports the new hostname. The fixtures
  deliberately include one such orphan per client.
- A short hostname that exists in more than one AD domain (2026-10-01 addition): an agent that reported a full DNS
  name is matched only to the AD object with exactly that `DNSHostName` (`match_method = fqdn_exact`); one that
  reported only the short name stays matched to all of them, with each row flagged `ambiguous_join_key` and counted in
  the summary. Both are exact comparisons, so this is still no fuzzy matching.
- A matched agent with no `last_seen` counts as stale, not covered (the conservative call).
- Rules out probabilistic matching without a new stage in the `.pipe()` chain.

## Evidence

- Code: [`src/agent_parity/correlation.py`](../../src/agent_parity/correlation.py) (`add_join_key`, `merge_with_agents`,
  `resolve_ambiguous_join_keys`, `classify_coverage`), `normalize_hostname` in [`src/agent_parity/models.py`](../../src/agent_parity/models.py)
- Tests: [`tests/test_models.py`](../../tests/test_models.py) `test_normalize_hostname`; [
  `tests/test_correlation.py`](../../tests/test_correlation.py) `test_hostname_normalization_matches_fqdn_and_case`,
  `test_merged_row_count_equals_union_of_join_keys`, `test_matched_agent_with_no_last_seen_is_stale_not_covered`; [
  `tests/test_pipeline_sync.py`](../../tests/test_pipeline_sync.py) `test_known_scenario_devices_classify_as_authored`
