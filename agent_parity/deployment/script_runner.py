"""Vendor-agnostic remote script execution.

Rather than agent-parity holding its own domain credentials to query Active
Directory, the AD export script is pushed to an already domain-joined,
already-managed endpoint. It executes there through the security vendor's own
remote scripting capability — the same trust relationship that is already in
place for the agent itself. Each connector implements the vendor mechanics
(stage / execute / poll / retrieve); this module is the uniform entry point
the pipeline calls.

**The storage-backed handoff itself lives in ``shared_tools.script_export``**
(``py-shared-tools``) now — ``run_ad_export`` is a thin wrapper supplying this
project's own script path, object-key prefix (``ad-exports``), expected CSV
header (``Name``), and error wording. It used to be a full implementation
here; ``credential-audit``'s own AD-metadata export handoff needed the exact
same orchestration (object storage mandatory for a live export, fixture mode
never touches it, same presigned-URL round trip), so the logic moved to
``py-shared-tools`` rather than staying duplicated under two names. See that
module's own docstring for the mechanics (why object storage instead of the
vendor's own output channel, what fixture mode does instead).
"""

from __future__ import annotations

from pathlib import Path

from shared_tools.script_export import ScriptExecutionError, run_script_export
from shared_tools.storage import ObjectStorage

from agent_parity.connectors.base import AgentConnector

AD_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "ad_sync" / "Export-ADDevices.ps1"

__all__ = ["AD_EXPORT_SCRIPT", "ScriptExecutionError", "run_ad_export"]


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
