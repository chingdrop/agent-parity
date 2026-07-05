"""agent-parity: device coverage reconciliation.

Correlates an Active Directory computer inventory against an EDR/security
agent inventory (SentinelOne, Carbon Black, or BitDefender) to find missing,
orphaned, and stale agent coverage. Vendor connectors, AD export parsing,
and the pandas correlation engine live here; no web framework, task queue,
or database. See ``agent_parity.pipeline`` for the two entrypoints
(``run_correlation`` for config.yaml + a connector, ``correlate_from_csvs``
for two CSVs with no configuration at all) and ``agent_parity.cli`` for the
standalone command line.
"""
