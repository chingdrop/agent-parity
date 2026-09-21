"""Vendor-agnostic remote script execution.

Rather than agent-parity holding its own domain credentials to query Active
Directory, the AD export script is pushed to an already domain-joined,
already-managed endpoint. It executes there through the security vendor's own
remote scripting capability — the same trust relationship that is already in
place for the agent itself. Each connector implements the vendor mechanics
(stage / execute / poll / retrieve); this module is the uniform entry point
the pipeline calls.

``run_script_export`` prefers a presigned-upload-URL handoff through object
storage over trusting the connector's own remote-execution output channel:
SentinelOne RSO's fetch-files and Carbon Black Live Response's command output
don't reliably preserve exact formatting (encoding, line-ending
normalization) and have real output-size limits, so a live run instead hands
the script a short-lived presigned PUT URL (``agent_parity.storage.ObjectStorage``)
and fetches the real output with a plain GET; the connector call's own return
value is discarded entirely. Fixture mode (a non-live connector) never
touches storage at all — there's no real endpoint to upload anything from.
``run_ad_export`` is the thin wrapper supplying this project's own script
path, object-key prefix (``ad-exports``), expected CSV header (``Name``), and
error wording.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agent_parity.connectors.base import AgentConnector, VendorConnector
from agent_parity.storage import ObjectStorage


class ScriptExecutionError(Exception):
    """Remote execution completed but did not produce usable output."""


def run_script_export(
    connector: VendorConnector,
    target_id: str,
    script_path: str | Path,
    *,
    storage: ObjectStorage | None = None,
    object_key: str | None = None,
    object_key_prefix: str = "exports",
    header_marker: str = "Name",
    what: str = "export",
) -> str:
    """Run ``script_path`` on ``target_id`` via ``connector`` and return its
    raw CSV text.

    ``storage`` may be ``None`` only when ``connector`` is not live (fixture
    mode); for a live connector, ``None`` is a configuration error, not a
    silent fallback to the vendor's own (unreliable) output channel.

    ``object_key_prefix`` scopes this call's objects within a shared bucket
    (e.g. ``"ad-exports"``, ``"ad-metadata"``) so two projects pointed at the
    same bucket never collide. ``header_marker`` is the column name expected
    in the exported CSV's first line — the cheap sanity check that this
    looks like the real export, not an error transcript. ``what`` only
    affects error-message wording (e.g. ``"AD export"``,
    ``"AD metadata export"``).
    """
    if not connector.is_live:
        raw = connector.deploy_and_run(script_path, target_id)
        return _validate_csv(connector.vendor, target_id, raw, header_marker, what)

    if storage is None:
        raise ScriptExecutionError(
            f"{connector.vendor}: object storage is required for a live {what} "
            f"(set STORAGE_BUCKET/STORAGE_ACCESS_KEY/STORAGE_SECRET_KEY) — the "
            f"vendor's remote-execution output channel doesn't reliably preserve "
            f"the exported CSV's formatting"
        )

    key = object_key or f"{object_key_prefix}/{connector.vendor}/{uuid4().hex}.csv"
    upload_url = storage.presigned_put_url(key)
    # The return value is intentionally unused here — the script's real
    # output *is* the uploaded object, never whatever the vendor channel
    # happened to capture as stdout.
    connector.deploy_and_run(script_path, target_id, script_args={"UploadUrl": upload_url})
    raw = storage.get_object(key).decode("utf-8")
    storage.delete_object(key)  # best-effort; never blocks a successful export
    return _validate_csv(connector.vendor, target_id, raw, header_marker, what)


def _validate_csv(vendor: str, target_id: str, raw: str, header_marker: str, what: str) -> str:
    if not raw or not raw.strip():
        raise ScriptExecutionError(f"{vendor}: {what} on {target_id!r} returned no output")
    # Cheap sanity check that we got the export, not an error transcript:
    # the script always emits a CSV header naming the expected column.
    if header_marker not in raw.splitlines()[0]:
        raise ScriptExecutionError(
            f"{vendor}: {what} output does not look like the expected CSV (first line: {raw.splitlines()[0]!r})"
        )
    return raw


AD_EXPORT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "Export-ADDevices.ps1"

__all__ = ["AD_EXPORT_SCRIPT", "ScriptExecutionError", "run_ad_export", "run_script_export"]


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
    return run_script_export(
        connector,
        target_id,
        script_path,
        storage=storage,
        object_key=object_key,
        object_key_prefix="ad-exports",
        header_marker="Name",
        what="AD export",
    )
