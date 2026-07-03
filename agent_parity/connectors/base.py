"""Shared connector interface.

Every vendor connector supports two capabilities:

* ``fetch_inventory()`` — pull the vendor's current endpoint list, normalized
  to ``AgentDevice`` records.
* ``deploy_and_run(script_path, target_id)`` — push a script to a managed
  endpoint through the vendor's remote-execution capability and return its
  stdout. This is how the AD export is collected: the script runs on an
  already domain-joined, already-managed endpoint, so agent-parity never
  needs its own domain credentials or LDAP bind.

When a connector has no usable credentials it falls back to local fixtures
under ``sample_data/<client>/`` so the whole pipeline runs with zero live
API access.
"""

from __future__ import annotations

import csv
import io
import json
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from typing import Callable, ClassVar

import requests

from agent_parity.models import AgentDevice


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
    seen = [d.last_seen for d in devices if d.last_seen]
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
    rows = list(reader)
    if not rows or column not in (reader.fieldnames or []):
        return csv_text
    parsed = {i: parse_timestamp(row.get(column)) for i, row in enumerate(rows)}
    stamps = [ts for ts in parsed.values() if ts]
    if not stamps:
        return csv_text
    shift = datetime.now(timezone.utc) - max(stamps)
    for i, row in enumerate(rows):
        if parsed[i]:
            row[column] = (parsed[i] + shift).isoformat()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
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

    #: Seconds between remote-execution status polls, and the overall cap.
    poll_interval: ClassVar[float] = 5.0
    poll_timeout: ClassVar[float] = 300.0

    def __init__(self, credentials: dict | None = None, fixture_dir: str | Path | None = None):
        self.credentials = credentials or {}
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.session = requests.Session()

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

    def deploy_and_run(self, script_path: str | Path, target_id: str) -> str:
        """Push a script to ``target_id``, execute it, and return its stdout."""
        if self.is_live:
            return self._live_deploy_and_run(Path(script_path), target_id)
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

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, url, timeout=30, **kwargs)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectorError(f"{self.vendor}: API request failed: {exc}") from exc
        return response

    # -- vendor-specific -----------------------------------------------------

    @abstractmethod
    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        """Normalize a raw inventory payload (live or fixture) to AgentDevice."""

    @abstractmethod
    def _live_fetch_inventory(self) -> list[AgentDevice]: ...

    @abstractmethod
    def _live_deploy_and_run(self, script_path: Path, target_id: str) -> str: ...
