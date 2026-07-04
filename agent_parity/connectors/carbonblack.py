"""Carbon Black Cloud connector.

Live mode is shaped after the Carbon Black Cloud public APIs:

* Inventory: ``POST /appservices/v6/orgs/{org_key}/devices/_search``.
* Remote execution: a Live Response session against the target device —
  create the session, ``put file`` to stage the script, ``create process``
  to run PowerShell against it, and read stdout from the session.

Carbon Black credentials are *per-client* scope: each client environment has
its own API ID / API secret key / org key, so the config resolver hands this
connector a different credential block per client.
"""

from __future__ import annotations

from pathlib import Path

from agent_parity.connectors.base import AgentConnector, ConnectorError, parse_timestamp
from agent_parity.models import AgentDevice, Vendor


class CarbonBlackConnector(AgentConnector):
    vendor = Vendor.CARBONBLACK.value
    required_credentials = ("api_url", "api_id", "api_key", "org_key")

    @property
    def _headers(self) -> dict:
        # CBC API keys authenticate as "<api_secret_key>/<api_id>".
        return {"X-Auth-Token": f"{self.credentials['api_key']}/{self.credentials['api_id']}"}

    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        devices = []
        for item in payload.get("results", []):
            devices.append(
                AgentDevice(
                    vendor=self.vendor,
                    agent_id=str(item.get("id", "")),
                    hostname=item.get("name", ""),
                    os=item.get("os_version", item.get("os", "")),
                    last_seen=parse_timestamp(item.get("last_contact_time")),
                    agent_version=item.get("sensor_version", ""),
                )
            )
        return devices

    def _live_fetch_inventory(self) -> list[AgentDevice]:
        base = self.credentials["api_url"].rstrip("/")
        org = self.credentials["org_key"]
        devices: list[AgentDevice] = []
        start, page = 0, 200
        while True:
            payload = self._request_json(
                "POST",
                f"{base}/appservices/v6/orgs/{org}/devices/_search",
                headers=self._headers,
                json={"criteria": {}, "start": start, "rows": page},
            )
            devices.extend(self._parse_inventory(payload))
            start += page
            if start >= payload.get("num_found", 0):
                return devices

    @staticmethod
    def _powershell_args(script_args: dict[str, str]) -> str:
        """Render ``script_args`` as ``-Name 'value'`` pairs for a raw command
        line (Live Response's ``create process`` takes one, unlike
        SentinelOne's structured ``inputParams``). Single quotes in a value
        are doubled per PowerShell's own quoting rules, not backslash-escaped.
        """
        if not script_args:
            return ""
        quote = "'"
        parts = (f"-{name} {quote}{value.replace(quote, quote * 2)}{quote}" for name, value in script_args.items())
        return " " + " ".join(parts)

    def _live_deploy_and_run(
        self, script_path: Path, target_id: str, script_args: dict[str, str]
    ) -> str:
        base = self.credentials["api_url"].rstrip("/")
        org = self.credentials["org_key"]
        lr = f"{base}/appservices/v6/orgs/{org}/liveresponse"

        # 1. Open a Live Response session against the device.
        session = self._request_json(
            "POST", f"{lr}/sessions", headers=self._headers, json={"device_id": int(target_id)}
        )
        session_id = session["id"]

        def session_ready() -> str | None:
            state = self._request_json(
                "GET", f"{lr}/sessions/{session_id}", headers=self._headers
            )
            return "ready" if state.get("status") == "ACTIVE" else None

        self._poll_until(session_ready, f"Live Response session on device {target_id}")

        remote_path = f"C:\\Windows\\Temp\\{script_path.name}"
        try:
            # 2. Stage the script on the endpoint (put file).
            with open(script_path, "rb") as fh:
                uploaded = self._request_json(
                    "POST", f"{lr}/sessions/{session_id}/files", headers=self._headers,
                    files={"file": (script_path.name, fh)},
                )
            self._request(
                "POST", f"{lr}/sessions/{session_id}/commands", headers=self._headers,
                json={"name": "put file", "file_id": uploaded["id"], "path": remote_path},
            )

            # 3. Run PowerShell against it and wait for the command to finish.
            # Live Response's "create process" takes a raw command line, so
            # script_args (e.g. the presigned upload URL) are appended
            # directly as -Name 'value' parameters rather than through any
            # structured input-parameters field.
            command = self._request_json(
                "POST", f"{lr}/sessions/{session_id}/commands", headers=self._headers,
                json={
                    "name": "create process",
                    "path": (
                        "powershell.exe -ExecutionPolicy Bypass -NonInteractive "
                        f"-File {remote_path}{self._powershell_args(script_args)}"
                    ),
                    "wait_for_completion": True,
                    "wait_for_output": True,
                },
            )
            command_id = command["id"]

            def command_output() -> str | None:
                state = self._request_json(
                    "GET", f"{lr}/sessions/{session_id}/commands/{command_id}",
                    headers=self._headers,
                )
                status = state.get("status")
                if status == "ERROR":
                    raise ConnectorError(f"{self.vendor}: Live Response command failed")
                if status != "COMPLETE":
                    return None
                return state.get("output", "")

            return self._poll_until(command_output, f"script output on device {target_id}")
        finally:
            # Always close the session; LR sessions are a limited resource.
            try:
                self._request("DELETE", f"{lr}/sessions/{session_id}", headers=self._headers)
            except ConnectorError:
                pass
