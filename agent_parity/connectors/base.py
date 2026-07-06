"""Shared connector interface.

Every vendor connector supports ``fetch_inventory()`` — pull the vendor's
current endpoint list, normalized to ``AgentDevice`` records. That's this
project's own domain logic; it stays here rather than in
``shared_tools.remote_exec``, which knows nothing about inventories or
``AgentDevice``.

Most connectors also support ``deploy_and_run(script_path, target_id)`` — push
a script to a managed endpoint through the vendor's remote-execution
capability and return its stdout. This is how the AD export is collected: the
script runs on an already domain-joined, already-managed endpoint, so
agent-parity never needs its own domain credentials or LDAP bind. **The
generic mechanics of this — credentialed HTTP via ``RestAdapter``, live/fixture
dispatch, polling, and the vendor registry — live in
``shared_tools.remote_exec.VendorConnector``**, shared with other projects
(``credential-audit``) that talk to the same kind of vendor remote-execution
APIs; ``AgentConnector`` here adds only what's specific to *this* project:
inventory fetching, and this project's own AD-export fixture behavior
(``_fixture_deploy_and_run``, keyed by domain controller CSV + timestamp
rebasing). Not every EDR vendor's real API exposes an equivalent to "run an
arbitrary script" though — connectors that don't (see
``supports_remote_execution``, inherited from ``VendorConnector``) are
fetch_inventory-only; ``pipeline.collect_ad_csv`` raises a clear error if the
configured vendor can't carry the AD export.

When a connector has no usable credentials it falls back to local fixtures
under ``sample_data/`` so the whole pipeline runs with zero live API access.
"""

from __future__ import annotations

import csv
import io
import json
from abc import abstractmethod
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from shared_tools.remote_exec import ConnectorError, ConnectorRegistry, VendorConnector

from agent_parity.models import AgentDevice, infer_machine_type, infer_platform

# infer_platform/infer_machine_type are re-exported here (not just imported
# for internal use) for existing call sites (carbonblack.py, bitdefender.py,
# seed_demo.py, tests) — the definitions live in agent_parity.models since
# correlation/engine.py needs them too, for AD-only rows, without pulling in
# this module's requests/RestAdapter dependency chain just for two pure
# string-processing functions.
__all__ = [
    "AgentConnector",
    "ConnectorError",
    "infer_machine_type",
    "infer_platform",
    "CONNECTOR_REGISTRY",
    "register_connector",
]


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


#: Vendor name (as used in config.yaml) -> connector class, populated by
#: @register_connector as each connector module is imported. Adding a new
#: vendor is "write a connector class decorated with @register_connector,
#: plus one import in connectors/__init__.py" — nothing else needs editing.
#: The registry mechanism itself (``ConnectorRegistry``) is shared via
#: ``shared_tools.remote_exec``; this instance is agent-parity's own, so an
#: unrelated project's vendor connectors never collide with these entries.
CONNECTOR_REGISTRY: ConnectorRegistry = ConnectorRegistry()
register_connector = CONNECTOR_REGISTRY.register


class AgentConnector(VendorConnector):
    """Base class for vendor connectors.

    Subclasses set ``vendor`` and ``required_credentials`` and implement the
    ``_live_*`` methods shaped after the vendor's real API. Credentialed HTTP,
    live/fixture dispatch for ``deploy_and_run``, and polling all come from
    ``shared_tools.remote_exec.VendorConnector`` (see ``session``, ``is_live``,
    ``_request``/``_request_json``/``_as_text``, ``_fixture_path``,
    ``_poll_until``); this class adds inventory fetching and this project's
    own AD-export fixture behavior.
    """

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

    # -- remote script execution: this project's fixture behavior -----------

    def _fixture_deploy_and_run(
            self, script_path: Path, target_id: str, script_args: dict[str, str]
    ) -> str:
        """The canned AD export for this specific domain controller stands in
        for the script output — one file per target_id, since a client with
        multiple AD domains has a distinct export per domain (see
        ``pipeline.collect_ad_frame``). ``script_args`` is ignored in fixture
        mode; there's no real script execution to parameterize.
        """
        path = self._fixture_path(f"ad_export_{target_id}.csv")
        return rebase_csv_timestamps(path.read_text())

    # -- vendor-specific -----------------------------------------------------

    @abstractmethod
    def _parse_inventory(self, payload: dict) -> list[AgentDevice]:
        """Normalize a raw inventory payload (live or fixture) to AgentDevice."""

    @abstractmethod
    def _live_fetch_inventory(self) -> list[AgentDevice]:
        ...
