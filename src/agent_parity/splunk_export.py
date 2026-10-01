"""Optional Splunk HTTP Event Collector forwarder.

Splunk is a *sink* here, never the system of record — the SQLite-backed run
history (`agent_parity.scheduling.db`/`persistence.py`) stays authoritative, and this
module only ships already-classified results for orgs that report out of
Splunk. Each run is sent whole, the way the original tool did it:

* **One event per row** of the classified frame (``SplunkConfig.sourcetype``).
  A dashboard shows the current state by filtering to the latest ``run_id``,
  and every field is searchable as-is.
* **One summary event per run** (``SplunkConfig.summary_sourcetype``) —
  coverage percentages and status counts — sent *after* the rows. Trend
  charts are then a single ``timechart`` over summaries, and a summary's
  presence marks its run as fully sent, so a dashboard keyed off the latest
  summary never shows a half-ingested run.

Every event carries the run's start time as its Splunk timestamp, so a run's
rows land at one instant. Why snapshots rather than per-run deltas: there is
no previous-run baseline to get wrong (a failed run, a vendor that dropped out
for one run), and the history needs no replaying to chart. Why per-row rather
than one event holding the whole frame: Splunk truncates events at 10,000
bytes and stops automatic JSON field extraction at 5,000 by default.

The module is a no-op unless both an HEC URL and token are configured. It
has no SQLAlchemy or pandas imports — callers hand it plain dicts; building
them from a run lives in ``agent_parity.scheduling.persistence``.
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


def send_run(rows: list[dict], summary: dict, splunk: SplunkConfig, *, event_time: float) -> int:
    """POST one run's row events, then its summary event, to HEC.

    Returns the number of events sent (rows + 1). ``event_time`` is the run's
    start as epoch seconds, applied to every event. Raises
    ``SplunkExportError`` on any HTTP failure; rows already sent stay sent,
    but the summary — the "run complete" marker — will not have been.
    """
    if not splunk.enabled:
        logger.debug("Splunk export disabled (no HEC URL/token configured); skipping")
        return 0

    envelopes = [_envelope(row, splunk.sourcetype, splunk, event_time) for row in rows]
    envelopes.append(_envelope(summary, splunk.summary_sourcetype, splunk, event_time))

    assert splunk.hec_url is not None  # noqa: S101 - type narrowing; guaranteed by splunk.enabled above
    url = splunk.hec_url.rstrip("/") + "/services/collector/event"
    headers = {"Authorization": f"Splunk {splunk.hec_token}"}
    for start in range(0, len(envelopes), BATCH_SIZE):
        # HEC accepts newline-concatenated event envelopes in one request.
        body = "\n".join(envelopes[start : start + BATCH_SIZE])
        try:
            response = requests.post(url, headers=headers, data=body, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SplunkExportError(f"HEC POST failed: {exc}") from exc

    logger.info("Forwarded %d row event(s) and a summary to Splunk", len(rows))
    return len(envelopes)


def _envelope(event: dict, sourcetype: str, splunk: SplunkConfig, event_time: float) -> str:
    return json.dumps(
        {
            "time": event_time,
            "index": splunk.index,
            "sourcetype": sourcetype,
            "source": "agent-parity",
            "event": event,
        }
    )
