"""SentinelOne Remote Script Orchestration (RSO) mixin.

Shared by any project's ``SentinelOneConnector`` that needs to push and run a
script via SentinelOne's RSO API — ``agent-parity``'s AD-export collection and
``credential-audit``'s AD-metadata collection both do the exact same upload ->
execute -> poll -> fetch-files sequence against the exact same endpoints, so
it lives here once rather than twice.

Shaped after the Management Console API v2.1 (public docs). Mix this in
alongside whatever project-specific base a connector needs — e.g.
``class SentinelOneConnector(SentinelOneRSOMixin, AgentConnector)`` — the
concrete class still sets its own ``vendor``/``required_credentials`` and
whatever else its own project needs (inventory fetching, or nothing at all);
this mixin provides only the RSO mechanics and assumes whatever it's mixed
into also provides ``agent_parity.shared.remote_exec.VendorConnector``'s
``credentials``/``_request_json``/``_request``/``_as_text``/``_poll_until``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from agent_parity.shared.remote_exec import ConnectorError

if TYPE_CHECKING:
    from collections.abc import Callable

    class _ConnectorLike(Protocol):
        """The full interface ``self`` needs at each of this mixin's own call
        sites — both what a cooperating ``VendorConnector``-derived base
        must supply (``credentials``, ``vendor``, ``_request*``,
        ``_as_text``, ``_poll_until``) and ``_headers``, which the mixin
        defines itself but which other methods here reach via ``self`` just
        the same. A type-checking-only stand-in, never instantiated or
        inherited from at runtime (see ``SentinelOneRSOMixin``'s own
        docstring for why the mixin itself deliberately doesn't inherit
        ``VendorConnector``).

        ``vendor`` MUST stay ``ClassVar[str]`` here, matching
        ``VendorConnector.vendor``'s own ``ClassVar`` — a plain ``str``
        looks equivalent but mypy treats a protocol's instance-attribute
        member and a ``ClassVar`` implementation as structurally
        incompatible, which silently breaks self-type matching for every
        other member on this protocol too (surfaced as a confusing
        "Invalid self argument" error on ``_headers``, not on ``vendor``
        itself).
        """

        credentials: dict
        vendor: ClassVar[str]

        @property
        def _headers(self) -> dict: ...
        def _request(self, method: str, url: str, **kwargs: object) -> dict | str | bytes: ...
        def _request_json(self, method: str, url: str, **kwargs: object) -> dict: ...
        @staticmethod
        def _as_text(payload: dict | str | bytes) -> str: ...
        def _poll_until(self, check: Callable[[], str | None], what: str) -> str: ...


class SentinelOneRSOMixin:
    """RSO mechanics only — credentials, ``is_live``, the HTTP session, and
    polling all come from whatever ``VendorConnector``-derived base the
    concrete class also inherits.

    Deliberately does not set ``vendor``/``required_credentials`` itself, so
    ``@register_connector`` always decorates a real, concrete connector
    class — never this shared mixin. For the same reason it doesn't actually
    inherit ``VendorConnector`` either (that would force a specific MRO on
    every consumer); each method instead declares an explicit ``_ConnectorLike``
    self-type so the type checker can still verify it, per mypy's documented
    pattern for mixins (https://mypy.readthedocs.io/en/stable/more_types.html#mixin-classes).
    """

    @property
    def _headers(self: _ConnectorLike) -> dict:
        return {"Authorization": f"ApiToken {self.credentials['api_token']}"}

    def _live_deploy_and_run(
        self: _ConnectorLike, script_path: Path, target_id: str, script_args: dict[str, str]
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
        # models passing values for those (e.g. a presigned upload URL for an
        # object-storage handoff — see agent-parity's deployment.script_runner).
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
