"""Shared connector interface.

Every vendor connector supports ``fetch_inventory()`` — pull the vendor's
current endpoint list, normalized to ``AgentDevice`` records.

Most also support ``deploy_and_run(script_path, target_id)`` — push a script
to a managed endpoint through the vendor's remote-execution capability and
return its stdout. This is how the AD export is collected: the script runs
on an already domain-joined, already-managed endpoint, so agent-parity never
needs its own domain credentials or LDAP bind. Not every EDR vendor's real
API exposes an equivalent to "run an arbitrary script" though — connectors
that don't (see ``supports_remote_execution`` below) are fetch_inventory-only,
and ``agent_parity.config.pick_ad_export_vendor`` is what picks a client's
AD-export vendor from among the ones that actually can.

When a connector has no usable credentials it falls back to local fixtures
under ``sample_data/<client>/`` so the whole pipeline runs with zero live
API access.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, ClassVar

import requests

from agent_parity.models import AgentDevice, infer_machine_type, infer_platform
from agent_parity.rest_adapter import RestAdapter, RestAdapterConfig

# infer_platform/infer_machine_type are re-exported here (not just imported
# for internal use) for existing call sites (carbonblack.py, bitdefender.py,
# seed_demo.py, tests) — the definitions live in agent_parity.models since
# correlation/engine.py needs them too, for AD-only rows, without pulling in
# this module's requests/RestAdapter dependency chain just for two pure
# string-processing functions.
__all__ = ["AgentConnector", "ConnectorError", "infer_machine_type", "infer_platform"]


class ConnectorError(Exception):
    """A vendor API call or remote execution failed."""


def parse_timestamp(value) -> datetime | None:
    """Parse the ISO-ish timestamps vendor APIs return into aware datetimes."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rebase_timestamps(devices: list[AgentDevice]) -> list[AgentDevice]:
    """Shift fixture timestamps so the newest ``last_seen`` is ~now.

    Fixture files contain static dates, which would otherwise all drift into
    "stale" as real time passes. Shifting every timestamp by the same delta
    preserves the *relative* ages that make the demo scenarios meaningful
    (a device authored as 30 days stale stays 30 days stale).
    Only used in fixture mode — live API data is never touched.
    """
    seen: list[datetime] = [d.last_seen for d in devices if d.last_seen is not None]
    if not seen:
        return devices
    shift = datetime.now(timezone.utc) - max(seen)
    return [
        replace(d, last_seen=d.last_seen + shift) if d.last_seen else d
        for d in devices
    ]


def rebase_csv_timestamps(csv_text: str, column: str = "LastLogonTimestamp") -> str:
    """Same rebasing as above, for the fixture AD export CSV."""
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames
    rows = list(reader)
    if not rows or not fieldnames or column not in fieldnames:
        return csv_text
    parsed = {i: parse_timestamp(row.get(column)) for i, row in enumerate(rows)}
    stamps: list[datetime] = [ts for ts in parsed.values() if ts is not None]
    if not stamps:
        return csv_text
    shift = datetime.now(timezone.utc) - max(stamps)
    for i, row in enumerate(rows):
        ts = parsed[i]
        if ts is not None:
            row[column] = (ts + shift).isoformat()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class AgentConnector(ABC):
    """Base class for vendor connectors.

    Subclasses set ``vendor`` and ``required_credentials`` and implement the
    ``_live_*`` methods shaped after the vendor's real API. The public
    methods decide between live and fixture mode.
    """

    vendor: ClassVar[str]
    required_credentials: ClassVar[tuple[str, ...]]

    #: Whether this vendor's real API exposes anything equivalent to "push
    #: and run an arbitrary script" (SentinelOne's Remote Script Orchestration,
    #: Carbon Black's Live Response). Not every EDR vendor does — BitDefender
    #: GravityZone's remote-task API is limited to predefined task types (scan,
    #: isolate, install/uninstall, ...), so it overrides this to False and is
    #: fetch_inventory-only. ``deploy_and_run`` refuses to run at all when this
    #: is False, in both live and fixture mode, so the pipeline never silently
    #: attributes a capability to a vendor that doesn't really have it.
    supports_remote_execution: ClassVar[bool] = True

    #: Seconds between remote-execution status polls, and the overall cap.
    poll_interval: ClassVar[float] = 5.0
    poll_timeout: ClassVar[float] = 300.0

    def __init__(self, credentials: dict | None = None, fixture_dir: str | Path | None = None):
        self.credentials = credentials or {}
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        # Call sites always pass fully-qualified URLs, so base_url is never
        # actually joined against — it only matters that RestAdapterConfig
        # requires one.
        self.session = RestAdapter(
            RestAdapterConfig(base_url=self.credentials.get("api_url") or ""),
            logger=logging.getLogger(f"agent_parity.connectors.{self.vendor}"),
        )

    @property
    def is_live(self) -> bool:
        return all(self.credentials.get(key) for key in self.required_credentials)

    # -- inventory ---------------------------------------------------------

    def fetch_inventory(self) -> list[AgentDevice]:
        if self.is_live:
            return self._live_fetch_inventory()
        return self._fixture_fetch_inventory()

    def _fixture_fetch_inventory(self) -> list[AgentDevice]:
        path = self._fixture_path(f"{self.vendor}_inventory.json")
        with open(path) as fh:
            payload = json.load(fh)
        return rebase_timestamps(self._parse_inventory(payload))

    # -- remote script execution -------------------------------------------

    def deploy_and_run(
            self,
            script_path: str | Path,
            target_id: str,
            script_args: dict[str, str] | None = None,
    ) -> str:
        """Push a script to ``target_id``, execute it, and return its stdout.

        ``script_args`` are passed through to the script itself — currently
        just the presigned upload URL when the AD export is handed off via
        object storage instead of the vendor's own output channel (see
        ``deployment.script_runner.run_ad_export``); ignored in fixture mode,
        where there's no real script execution to parameterize.

        Checked before the live/fixture fork so a vendor without genuine
        remote-execution capability can't produce a misleadingly successful
        result in demo mode either.
        """
        if not self.supports_remote_execution:
            raise ConnectorError(
                f"{self.vendor}: does not support remote script execution "
                f"(fetch_inventory-only vendor)"
            )
        if self.is_live:
            return self._live_deploy_and_run(Path(script_path), target_id, script_args or {})
        # Fixture mode: the canned AD export stands in for the script output.
        path = self._fixture_path("ad_export.csv")
        return rebase_csv_timestamps(path.read_text())

    # -- helpers -------------------------------------------------------------

    def _fixture_path(self, filename: str) -> Path:
        if not self.fixture_dir:
            raise ConnectorError(
                f"{self.vendor}: no credentials configured and no fixture_dir provided"
            )
        path = self.fixture_dir / filename
        if not path.exists():
            raise ConnectorError(f"{self.vendor}: fixture not found: {path}")
        return path

    def _poll_until(self, check: Callable[[], str | None], what: str) -> str:
        """Poll ``check`` until it returns output or the timeout elapses."""
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            result = check()
            if result is not None:
                return result
            time.sleep(self.poll_interval)
        raise ConnectorError(f"{self.vendor}: timed out waiting for {what}")

    def _request(self, method: str, url: str, **kwargs) -> dict | str | bytes:
        """Issue a request through the shared RestAdapter (retries included).

        Returns already-parsed content — a dict for JSON responses, str for
        text/html, raw bytes otherwise — not a ``requests.Response``.
        """
        try:
            return self.session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise ConnectorError(f"{self.vendor}: API request failed: {exc}") from exc

    @staticmethod
    def _as_text(payload: dict | str | bytes) -> str:
        """Coerce a ``_request`` result into text, for script-output call sites."""
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
        raise ConnectorError(f"expected text output, got parsed JSON: {payload!r}")

    def _request_json(self, method: str, url: str, **kwargs) -> dict:
        """Like ``_request``, but for endpoints that always return a JSON object."""
        payload = self._request(method, url, **kwargs)
        if not isinstance(payload, dict):
            raise ConnectorError(f"{self.vendor}: expected a JSON object, got {payload!r}")
        return payload

    # -- vendor-specific -----------------------------------------------------

    @abstractmethod
    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        """Normalize a raw inventory payload (live or fixture) to AgentDevice."""

    @abstractmethod
    def _live_fetch_inventory(self) -> list[AgentDevice]:
        ...

    def _live_deploy_and_run(
            self, script_path: Path, target_id: str, script_args: dict[str, str]
    ) -> str:
        """Default for vendors with ``supports_remote_execution = False``.

        The public ``deploy_and_run`` already refuses before reaching here;
        this is a defensive fallback for anything that calls this directly.
        Vendors that genuinely support remote execution override it.
        """
        raise ConnectorError(f"{self.vendor}: remote script execution not implemented")
