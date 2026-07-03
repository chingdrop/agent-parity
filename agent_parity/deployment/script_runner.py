"""Vendor-agnostic remote script execution.

Rather than agent-parity holding its own domain credentials to query Active
Directory, the AD export script is pushed to an already domain-joined,
already-managed endpoint and executed through the security vendor's own
remote scripting capability — the same trust relationship that is already in
place for the agent itself. Each connector implements the vendor mechanics
(stage / execute / poll / retrieve); this module is the uniform entry point
the pipeline calls.
"""

from __future__ import annotations

from pathlib import Path

from agent_parity.connectors.base import AgentConnector

AD_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "ad_sync" / "Export-ADDevices.ps1"


class ScriptExecutionError(Exception):
    """Remote execution completed but did not produce usable output."""


def run_ad_export(
        connector: AgentConnector,
        target_id: str,
        script_path: str | Path = AD_EXPORT_SCRIPT,
) -> str:
    """Run the AD export script on ``target_id`` and return its raw CSV stdout.

    The connector decides live vs. fixture mode; either way the caller gets
    the same thing back — CSV text ready for ``ad_sync.parser``.
    """
    raw = connector.deploy_and_run(script_path, target_id)
    if not raw or not raw.strip():
        raise ScriptExecutionError(
            f"{connector.vendor}: AD export on {target_id!r} returned no output"
        )
    # Cheap sanity check that we got the export, not an error transcript:
    # the script always emits a CSV header naming the hostname column.
    if "Name" not in raw.splitlines()[0]:
        raise ScriptExecutionError(
            f"{connector.vendor}: AD export output does not look like the "
            f"expected CSV (first line: {raw.splitlines()[0]!r})"
        )
    return raw
