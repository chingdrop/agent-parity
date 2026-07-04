"""SentinelOne connector.

Live mode is shaped after the Management Console API v2.1 (public docs):

* Inventory: ``GET /web/api/v2.1/agents`` with cursor pagination.
* Remote execution: Remote Script Orchestration — upload the script to the
  script library, execute it against a target agent, poll
  ``/web/api/v2.1/remote-scripts/status``, then fetch the result artifact.

SentinelOne credentials are *global* scope: one API token covers every site
in the organization, so every client resolves to the same credential set.
"""

from __future__ import annotations

from pathlib import Path

from agent_parity.connectors.base import AgentConnector, ConnectorError, parse_timestamp
from agent_parity.models import AgentDevice, Vendor


class SentinelOneConnector(AgentConnector):
    vendor = Vendor.SENTINELONE.value
    required_credentials = ("api_url", "api_token")

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"ApiToken {self.credentials['api_token']}"}

    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        devices = []
        for item in payload.get("data", []):
            devices.append(
                AgentDevice(
                    vendor=self.vendor,
                    agent_id=str(item.get("id", "")),
                    hostname=item.get("computerName", ""),
                    os=item.get("osName", ""),
                    last_seen=parse_timestamp(item.get("lastActiveDate")),
                    agent_version=item.get("agentVersion", ""),
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

    def _live_deploy_and_run(
        self, script_path: Path, target_id: str, script_args: dict[str, str]
    ) -> str:
        base = self.credentials["api_url"].rstrip("/")

        # 1. Upload the script to the script library.
        with open(script_path, "rb") as fh:
            upload = self._request_json(
                "POST",
                f"{base}/web/api/v2.1/remote-scripts",
                headers=self._headers,
                files={"file": (script_path.name, fh)},
                data={"scriptType": "action", "osTypes": "windows"},
            )
        script_id = upload["data"]["id"]

        # 2. Execute it against the target agent. RSO scripts can declare
        # user-facing input parameters in the script library; "inputParams"
        # models passing values for those (e.g. the presigned upload URL for
        # the object-storage handoff — see deployment.script_runner).
        execution = self._request_json(
            "POST",
            f"{base}/web/api/v2.1/remote-scripts/execute",
            headers=self._headers,
            json={
                "filter": {"ids": [target_id]},
                "data": {
                    "scriptId": script_id,
                    "outputDestination": "SentinelCloud",
                    "inputParams": script_args,
                },
            },
        )
        task_id = execution["data"]["parentTaskId"]

        # 3. Poll task status until the run finishes, then fetch the output.
        def check() -> str | None:
            status = self._request_json(
                "GET",
                f"{base}/web/api/v2.1/remote-scripts/status",
                headers=self._headers,
                params={"parentTaskId": task_id},
            )
            tasks = status.get("data", [])
            if not tasks:
                return None
            state = tasks[0].get("status")
            if state in ("failed", "canceled", "expired"):
                raise ConnectorError(f"{self.vendor}: remote script {state} on {target_id}")
            if state != "completed":
                return None
            result = self._request(
                "GET",
                f"{base}/web/api/v2.1/remote-scripts/fetch-files",
                headers=self._headers,
                params={"taskId": tasks[0]["id"]},
            )
            return self._as_text(result)

        return self._poll_until(check, f"remote script on agent {target_id}")
