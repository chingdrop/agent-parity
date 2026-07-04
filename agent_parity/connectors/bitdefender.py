"""BitDefender GravityZone connector.

Live mode is shaped after the GravityZone Control Center JSON-RPC API:

* Inventory: the ``getEndpointsList`` method on the ``network`` service.
* Remote execution: the task pattern — create a custom task targeting the
  endpoint, poll ``getTaskStatus`` until it finishes, then fetch the task
  output. (GravityZone's real API exposes task creation per task type, e.g.
  ``createScanTask``; the custom-script variant here follows that shape.)

Authentication is HTTP Basic with the API key as the username.
"""

from __future__ import annotations

import base64
import itertools
from pathlib import Path

from agent_parity.connectors.base import AgentConnector, ConnectorError, parse_timestamp
from agent_parity.models import AgentDevice, Vendor


class BitDefenderConnector(AgentConnector):
    vendor = Vendor.BITDEFENDER.value
    required_credentials = ("api_url", "api_key")

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
            devices.append(
                AgentDevice(
                    vendor=self.vendor,
                    agent_id=str(item.get("id", "")),
                    hostname=item.get("name", ""),
                    os=item.get("operatingSystemVersion", ""),
                    last_seen=parse_timestamp(item.get("lastSeen")),
                    agent_version=(item.get("agent") or {}).get("version", ""),
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

    def _live_deploy_and_run(self, script_path: Path, target_id: str) -> str:
        # 1. Create a custom-script task targeting the endpoint.
        script_body = base64.b64encode(script_path.read_bytes()).decode()
        created = self._rpc(
            "network",
            "createCustomScriptTask",
            {
                "targetIds": [target_id],
                "scriptName": script_path.name,
                "scriptContent": script_body,
                "interpreter": "powershell",
            },
        )
        task_id = created.get("taskId") or created.get("id")
        if not task_id:
            raise ConnectorError(f"{self.vendor}: task creation returned no task id")

        # 2. Poll task status (1=pending, 2=in progress, 3=finished per the
        #    GravityZone task status convention), then fetch the output.
        def check() -> str | None:
            status = self._rpc("network", "getTaskStatus", {"taskId": task_id})
            state = status.get("status")
            if state in (4, "failed"):
                raise ConnectorError(f"{self.vendor}: task failed on endpoint {target_id}")
            if state not in (3, "finished"):
                return None
            output = self._rpc("network", "getTaskOutput", {"taskId": task_id})
            return output.get("output", "")

        return self._poll_until(check, f"task on endpoint {target_id}")
