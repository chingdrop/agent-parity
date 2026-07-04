"""Vendor-agnostic remote script execution.

Rather than agent-parity holding its own domain credentials to query Active
Directory, the AD export script is pushed to an already domain-joined,
already-managed endpoint. It executes there through the security vendor's own
remote scripting capability — the same trust relationship that is already in
place for the agent itself. Each connector implements the vendor mechanics
(stage / execute / poll / retrieve); this module is the uniform entry point
the pipeline calls.

There are two ways the script's CSV output gets back to agent-parity, chosen
by whether object storage (``agent_parity.storage``) is configured:

* **No storage (default)** — the vendor's own remote-execution channel
  captures the script's stdout directly and that *is* the CSV, exactly as
  before object storage existed.
* **Storage configured** — the script never returns the CSV through the
  vendor channel at all. A short-lived presigned PUT URL is generated, the
  script receives it as an argument and uploads its own output straight to
  object storage, the vendor call only needs to report that the script *ran*
  (its stdout is ignored), and the CSV is fetched with a plain GET. This
  exists because vendor remote-execution channels have real output-size
  limits a full AD export can exceed — with storage configured, the export
  never has to fit through that channel at all.

Only engaged when the connector is actually live: fixture mode has no real
endpoint to run a script on, so there's nothing that could genuinely upload
anything — it always returns the canned fixture CSV directly, regardless of
whether storage happens to be configured.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agent_parity.connectors.base import AgentConnector
from agent_parity.storage import ObjectStorage

AD_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "ad_sync" / "Export-ADDevices.ps1"


class ScriptExecutionError(Exception):
    """Remote execution completed but did not produce usable output."""


def run_ad_export(
    connector: AgentConnector,
    target_id: str,
    script_path: str | Path = AD_EXPORT_SCRIPT,
    storage: ObjectStorage | None = None,
    object_key: str | None = None,
) -> str:
    """Run the AD export script on ``target_id`` and return its raw CSV text."""
    if storage is None or not connector.is_live:
        raw = connector.deploy_and_run(script_path, target_id)
        return _validate_csv(connector.vendor, target_id, raw)

    key = object_key or f"ad-exports/{connector.vendor}/{uuid4().hex}.csv"
    upload_url = storage.presigned_put_url(key)
    # The return value is intentionally unused here — with storage
    # configured, the script's real output *is* the uploaded object, not
    # whatever the vendor channel happened to capture as stdout.
    connector.deploy_and_run(script_path, target_id, script_args={"UploadUrl": upload_url})
    raw = storage.get_object(key).decode("utf-8")
    storage.delete_object(key)  # best-effort; never blocks a successful export
    return _validate_csv(connector.vendor, target_id, raw)


def _validate_csv(vendor: str, target_id: str, raw: str) -> str:
    if not raw or not raw.strip():
        raise ScriptExecutionError(f"{vendor}: AD export on {target_id!r} returned no output")
    # Cheap sanity check that we got the export, not an error transcript:
    # the script always emits a CSV header naming the hostname column.
    if "Name" not in raw.splitlines()[0]:
        raise ScriptExecutionError(
            f"{vendor}: AD export output does not look like the "
            f"expected CSV (first line: {raw.splitlines()[0]!r})"
        )
    return raw
