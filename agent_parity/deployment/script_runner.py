"""Vendor-agnostic remote script execution.

Rather than agent-parity holding its own domain credentials to query Active
Directory, the AD export script is pushed to an already domain-joined,
already-managed endpoint. It executes there through the security vendor's own
remote scripting capability — the same trust relationship that is already in
place for the agent itself. Each connector implements the vendor mechanics
(stage / execute / poll / retrieve); this module is the uniform entry point
the pipeline calls.

**Object storage (``shared_tools.storage``, via the ``vendor/py-shared-tools``
submodule) is mandatory for any live export.**
Vendor remote-execution output channels (SentinelOne RSO's fetch-files,
Carbon Black Live Response's command output) don't reliably preserve a CSV's
exact formatting — encoding and line-ending normalization, truncation at
real output-size limits — observed problems, not theoretical ones. Instead:
a short-lived presigned PUT URL is generated, the script receives it as an
argument and uploads its own output straight to object storage, the vendor
call only needs to report that the script *ran* (its stdout is ignored
entirely), and the CSV is fetched with a plain GET. The uploaded bytes are
exactly what the script wrote — the vendor's output-capture path is never in
the loop for the data itself.

The one exception is fixture mode: a non-live connector has no real endpoint
to run a script on, so there's nothing that could genuinely upload anything.
It always returns the canned fixture CSV directly, regardless of whether
storage happens to be configured — storage is only required once a
connector is actually live.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agent_parity.connectors.base import AgentConnector
from shared_tools.storage import ObjectStorage

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
    """Run the AD export script on ``target_id`` and return its raw CSV text.

    ``storage`` may be ``None`` only when ``connector`` is not live (fixture
    mode); for a live connector, ``None`` is a configuration error, not a
    silent fallback to the vendor's own (unreliable) output channel.
    """
    if not connector.is_live:
        raw = connector.deploy_and_run(script_path, target_id)
        return _validate_csv(connector.vendor, target_id, raw)

    if storage is None:
        raise ScriptExecutionError(
            f"{connector.vendor}: object storage is required for a live AD export "
            f"(set STORAGE_BUCKET/STORAGE_ACCESS_KEY/STORAGE_SECRET_KEY) — the "
            f"vendor's remote-execution output channel doesn't reliably preserve "
            f"the exported CSV's formatting"
        )

    key = object_key or f"ad-exports/{connector.vendor}/{uuid4().hex}.csv"
    upload_url = storage.presigned_put_url(key)
    # The return value is intentionally unused here — the script's real
    # output *is* the uploaded object, never whatever the vendor channel
    # happened to capture as stdout.
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
