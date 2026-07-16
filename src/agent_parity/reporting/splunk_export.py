"""Optional Splunk HTTP Event Collector forwarder.

Splunk is a *sink* here, never the system of record — the SQLite-backed run
history (`agent_parity.db`/`persistence.py`) stays authoritative, and this
module only ships already-classified results for orgs that centralize
alerting in Splunk. Two deliberate choices follow from that:

* We emit **deltas, not snapshots**: only state transitions since the
  previous run (e.g. a device moving from ``covered`` to ``missing_agent``).
  Re-indexing every device every run would just bloat the license for data
  the run history already has.
* Events are structured for Splunk's model: well-formed JSON with an explicit
  ``sourcetype`` and a dedicated ``index`` (never ``main``), so nothing on
  the Splunk side ever needs to re-derive classification logic in SPL.

The module is a no-op unless both an HEC URL and token are configured.

This module has no SQLAlchemy imports either — callers hand us plain delta
dicts; the diffing against run history lives in ``agent_parity.persistence``.
"""

from __future__ import annotations

import json
import logging

import requests

from agent_parity.config import SplunkConfig

logger = logging.getLogger(__name__)

#: HEC batches are size-limited; 100 events per POST keeps us well clear.
BATCH_SIZE = 100


class SplunkExportError(Exception):
    pass


def send_deltas(deltas: list[dict], splunk: SplunkConfig) -> int:
    """POST delta events to HEC. Returns the number of events sent.

    Each delta dict is one transition event, e.g.::

        {"client": "acme", "join_key": "acme-ws-014", "vendor": "sentinelone",
         "previous_status": "covered", "status": "missing_agent",
         "run_id": 7, "run_started_at": "2026-07-03T00:00:00+00:00"}
    """
    if not splunk.enabled:
        logger.debug("Splunk export disabled (no HEC URL/token configured); skipping")
        return 0
    if not deltas:
        return 0

    assert splunk.hec_url is not None  # guaranteed by splunk.enabled above
    url = splunk.hec_url.rstrip("/") + "/services/collector/event"
    headers = {"Authorization": f"Splunk {splunk.hec_token}"}
    sent = 0
    for start in range(0, len(deltas), BATCH_SIZE):
        batch = deltas[start: start + BATCH_SIZE]
        # HEC accepts newline-concatenated event envelopes in one request.
        body = "\n".join(
            json.dumps(
                {
                    "index": splunk.index,
                    "sourcetype": splunk.sourcetype,
                    "source": "agent-parity",
                    "event": delta,
                }
            )
            for delta in batch
        )
        try:
            response = requests.post(url, headers=headers, data=body, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SplunkExportError(f"HEC POST failed: {exc}") from exc
        sent += len(batch)

    logger.info("Forwarded %d coverage delta event(s) to Splunk", sent)
    return sent
