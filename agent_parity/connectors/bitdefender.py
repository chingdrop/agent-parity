"""BitDefender GravityZone connector.

Live mode is shaped after the GravityZone Control Center JSON-RPC API:

* Inventory: the ``getEndpointsList`` method on the ``network`` service.

That's it — this connector is deliberately fetch_inventory-only
(``supports_remote_execution = False``). GravityZone's remote-task API is
real, but it's limited to predefined task types (scan, isolate/deisolate,
install/uninstall, patch management, ...); it doesn't expose anything
equivalent to SentinelOne's Remote Script Orchestration or Carbon Black's
Live Response for pushing and running an arbitrary script. An earlier
version of this connector modeled a ``createCustomScriptTask`` RPC method to
fill that gap, but that was an invented extrapolation, not a documented
GravityZone capability, so it's been removed rather than left implying an
accuracy it doesn't have. Clients on BitDefender need at least one other
enabled vendor to carry their AD export (see
``agent_parity.config.pick_ad_export_vendor``).

Authentication is HTTP Basic with the API key as the username.
"""

from __future__ import annotations

import base64
import itertools

from agent_parity.connectors.base import (
    AgentConnector,
    ConnectorError,
    infer_platform,
    parse_timestamp,
)
from agent_parity.models import AgentDevice, Vendor

# GravityZone's machineType is a numeric enum (per its own API); this maps it
# to SentinelOne's string wording ("server" / "desktop") rather than
# inferring from OS text, since BitDefender does report this directly.
_MACHINE_TYPES = {1: "desktop", 2: "server"}


class BitDefenderConnector(AgentConnector):
    vendor = Vendor.BITDEFENDER.value
    required_credentials = ("api_url", "api_key")
    supports_remote_execution = False

    _rpc_ids = itertools.count(1)

    @property
    def _headers(self) -> dict:
        token = base64.b64encode(f"{self.credentials['api_key']}:".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _rpc(self, service: str, method: str, params: dict) -> dict:
        base = self.credentials["api_url"].rstrip("/")
        payload = self._request_json(
            "POST",
            f"{base}/api/v1.0/jsonrpc/{service}",
            headers=self._headers,
            json={
                "jsonrpc": "2.0",
                "id": next(self._rpc_ids),
                "method": method,
                "params": params,
            },
        )
        if payload.get("error"):
            raise ConnectorError(f"{self.vendor}: RPC {method} failed: {payload['error']}")
        return payload.get("result") or {}

    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        items = (payload.get("result") or payload).get("items", [])
        devices = []
        for item in items:
            os_version = item.get("operatingSystemVersion", "")
            devices.append(
                AgentDevice(
                    vendor=self.vendor,
                    agent_id=str(item.get("id", "")),
                    hostname=item.get("name", ""),
                    os=os_version,
                    last_seen=parse_timestamp(item.get("lastSeen")),
                    agent_version=(item.get("agent") or {}).get("version", ""),
                    # GravityZone has no equivalent to SentinelOne's osType
                    # field, so it's inferred from the OS name text instead.
                    platform=infer_platform(os_version),
                    # GravityZone's own machineType is a numeric enum;
                    # translated to SentinelOne's string wording.
                    machine_type=_MACHINE_TYPES.get(item.get("machineType"), ""),
                )
            )
        return devices

    def _live_fetch_inventory(self) -> list[AgentDevice]:
        devices: list[AgentDevice] = []
        page = 1
        while True:
            result = self._rpc(
                "network", "getEndpointsList", {"page": page, "perPage": 100}
            )
            devices.extend(self._parse_inventory({"result": result}))
            if page >= int(result.get("pagesCount", 1)):
                return devices
            page += 1
