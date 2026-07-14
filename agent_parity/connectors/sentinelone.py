"""SentinelOne connector.

Live mode is shaped after the Management Console API v2.1 (public docs):

* Inventory: ``GET /web/api/v2.1/agents`` with cursor pagination.
* Remote execution: Remote Script Orchestration — upload the script to the
  script library, execute it against a target agent, poll
  ``/web/api/v2.1/remote-scripts/status``, then fetch the result artifact.
  **These RSO mechanics (``_headers``/``_live_deploy_and_run``) live in
  ``shared_tools.sentinelone.SentinelOneRSOMixin``**, shared verbatim with
  ``credential-audit``'s own ``SentinelOneConnector`` rather than duplicated —
  both projects push a script and poll the exact same endpoints, the only
  difference is what each does with the result. This module adds only the
  inventory-fetching half, which is agent-parity-specific.

Inventory records also carry an ``osRevision``-style field with the agent's
exact Windows build number — reported separately from ``osName``'s coarse
product name, and (per direct prior experience with this API, not just
public docs) needs to be parsed out of the raw string rather than read as a
clean value; ``extract_build_number`` handles that regardless of the exact
raw shape. The precise current field name is a best-effort reconstruction,
not verified against a live tenant at write time — worth confirming against
current API docs before relying on it. Carbon Black and BitDefender have no
equivalent field, so their connectors don't set ``os_build`` at all.
"""

from __future__ import annotations

from shared_tools.sentinelone import SentinelOneRSOMixin

from agent_parity.connectors.base import AgentConnector, parse_timestamp, register_connector
from agent_parity.models import AgentDevice, Vendor
from agent_parity.os_eol import extract_build_number


@register_connector
class SentinelOneConnector(SentinelOneRSOMixin, AgentConnector):
    vendor = Vendor.SENTINELONE.value
    required_credentials = ("api_url", "api_token")
    # Covered the bulk of the original client base — preferred over Carbon
    # Black when both are capable of carrying a client's AD export.
    ad_export_priority = 0

    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        devices = []
        for item in payload.get("data", []):
            devices.append(
                AgentDevice(
                    vendor=self.vendor,
                    agent_id=str(item.get("id", "")),
                    hostname=item.get("computerName", ""),
                    os=item.get("osName", ""),
                    os_build=extract_build_number(item.get("osRevision")),
                    last_seen=parse_timestamp(item.get("lastActiveDate")),
                    agent_version=item.get("agentVersion", ""),
                    # SentinelOne's own wording is the canonical one other
                    # connectors normalize to — passed straight through here.
                    platform=item.get("osType", ""),
                    machine_type=item.get("machineType", ""),
                )
            )
        return devices

    def _live_fetch_inventory(self) -> list[AgentDevice]:
        base = self.credentials["api_url"].rstrip("/")
        devices: list[AgentDevice] = []
        cursor = None
        while True:
            params: dict[str, object] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = self._request_json(
                "GET", f"{base}/web/api/v2.1/agents", headers=self._headers, params=params
            )
            devices.extend(self._parse_inventory(payload))
            cursor = (payload.get("pagination") or {}).get("nextCursor")
            if not cursor:
                return devices
