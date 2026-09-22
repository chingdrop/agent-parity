"""Shared connector interface.

Every vendor connector supports ``fetch_inventory()`` — pull the vendor's
current endpoint list, normalized to ``AgentDevice`` records. That's this
project's own domain logic; it lives on ``AgentConnector``, while
``VendorConnector`` (below) knows nothing about inventories or ``AgentDevice``.

Most connectors also support ``deploy_and_run(script_path, target_id)`` — push
a script to a managed endpoint through the vendor's remote-execution
capability and return its stdout. This is how the AD export is collected: the
script runs on an already domain-joined, already-managed endpoint, so
agent-parity never needs its own domain credentials or LDAP bind. **The
generic mechanics of this — credentialed HTTP via ``RestAdapter``, live/fixture
dispatch, polling, and the vendor registry — live in ``VendorConnector``**;
``AgentConnector`` adds only what's specific to *this* project:
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
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import requests

from agent_parity.models import AgentDevice, infer_machine_type, infer_platform
from agent_parity.shared.rest_adapter import RestAdapter, RestAdapterConfig

# infer_platform/infer_machine_type are re-exported here (not just imported
# for internal use) for existing call sites (carbonblack.py, bitdefender.py,
# tests) — the definitions live in agent_parity.models since
# correlation.py needs them too, for AD-only rows, without pulling in
# this module's requests/RestAdapter dependency chain just for two pure
# string-processing functions.
__all__ = [
    "AgentConnector",
    "ConnectorError",
    "ConnectorRegistry",
    "VendorConnector",
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
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    shift = datetime.now(UTC) - max(seen)
    return [replace(d, last_seen=d.last_seen + shift) if d.last_seen else d for d in devices]


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
    shift = datetime.now(UTC) - max(stamps)
    for i, row in enumerate(rows):
        ts = parsed[i]
        if ts is not None:
            row[column] = (ts + shift).isoformat()
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class ConnectorError(Exception):
    """A vendor API call, remote execution, or fixture lookup failed."""


class ConnectorRegistry(dict):
    """vendor-name -> connector class, keyed by each registered class's own
    ``vendor`` attribute.

    agent-parity owns one instance, ``CONNECTOR_REGISTRY`` below.
    """

    def register(self, cls: type[VendorConnector]) -> type[VendorConnector]:
        """Class decorator: adds ``cls`` to this registry under its own
        ``vendor`` attribute. Bind a project-local name to the bound method
        (``register_connector = CONNECTOR_REGISTRY.register``) to use it as
        ``@register_connector`` at each connector class definition.
        """
        self[cls.vendor] = cls
        return cls


class VendorConnector(ABC):
    """Base class for a vendor's security-console API.

    Subclasses set ``vendor``/``required_credentials`` and implement whatever
    ``_live_*``/fixture hook methods their own domain needs (``AgentConnector``
    adds ``fetch_inventory``); this class only provides what every vendor
    connector needs regardless of what it fetches: the ``RestAdapter``
    session, credential-gated live/fixture dispatch, and — for vendors that
    support it — remote script execution.

    Live/fixture mode is a first-class fork here, not a test-only shim: when
    a connector has no usable credentials (``is_live`` is false), every
    public method is expected to fall back to canned fixture data instead of
    raising — that's what lets a fresh checkout run a full pipeline with
    zero live API access. What "canned data" looks like (file naming, any
    post-processing) is delegated to hook methods a subclass implements.
    """

    vendor: ClassVar[str]
    required_credentials: ClassVar[tuple[str, ...]]

    #: Whether this vendor's real API exposes anything equivalent to "push
    #: and run an arbitrary script" (SentinelOne's Remote Script
    #: Orchestration, Carbon Black's Live Response). Not every vendor does —
    #: set this ``False`` for one that doesn't. ``deploy_and_run`` refuses
    #: before the live/fixture fork when this is ``False``, so a pipeline
    #: can't accidentally "succeed" at something the vendor doesn't really
    #: support, even in fixture mode.
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
            logger=logging.getLogger(f"{type(self).__module__}.{self.vendor}"),
        )

    @property
    def is_live(self) -> bool:
        return all(self.credentials.get(key) for key in self.required_credentials)

    # -- remote script execution -------------------------------------------

    def deploy_and_run(
        self,
        script_path: str | Path,
        target_id: str,
        script_args: dict[str, str] | None = None,
    ) -> str:
        """Push a script to ``target_id``, execute it, and return its stdout.

        Checked before the live/fixture fork so a vendor without genuine
        remote-execution capability can't produce a misleadingly successful
        result in demo mode either.
        """
        if not self.supports_remote_execution:
            raise ConnectorError(
                f"{self.vendor}: does not support remote script execution (fetch_inventory-only vendor)"
            )
        if self.is_live:
            return self._live_deploy_and_run(Path(script_path), target_id, script_args or {})
        return self._fixture_deploy_and_run(Path(script_path), target_id, script_args or {})

    def _live_deploy_and_run(self, script_path: Path, target_id: str, script_args: dict[str, str]) -> str:
        """Default for a vendor that hasn't implemented live remote execution.

        The public ``deploy_and_run`` already refuses before reaching here
        for a vendor with ``supports_remote_execution = False``; this is a
        defensive fallback for a vendor that supports it but genuinely hasn't
        overridden this yet.
        """
        raise ConnectorError(f"{self.vendor}: remote script execution not implemented")

    def _fixture_deploy_and_run(self, script_path: Path, target_id: str, script_args: dict[str, str]) -> str:
        """Canned stand-in for a live remote-execution run.

        What "canned" means (file naming, any post-processing) is
        project-specific — any subclass with ``supports_remote_execution =
        True`` must override this.
        """
        raise ConnectorError(
            f"{self.vendor}: no fixture behavior defined for deploy_and_run (override _fixture_deploy_and_run)"
        )

    # -- helpers -------------------------------------------------------------

    def _fixture_path(self, filename: str) -> Path:
        if not self.fixture_dir:
            raise ConnectorError(f"{self.vendor}: no credentials configured and no fixture_dir provided")
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


#: Vendor name (as used in config.yaml) -> connector class, populated by
#: @register_connector as each connector module is imported. Adding a new
#: vendor is "write a connector class decorated with @register_connector,
#: plus one import in connectors/__init__.py" — nothing else needs editing.
#: ``ConnectorRegistry`` is defined above; this is the instance connectors
#: register into.
CONNECTOR_REGISTRY: ConnectorRegistry = ConnectorRegistry()
register_connector = CONNECTOR_REGISTRY.register


class AgentConnector(VendorConnector):
    """Base class for vendor connectors.

    Subclasses set ``vendor`` and ``required_credentials`` and implement the
    ``_live_*`` methods shaped after the vendor's real API. Credentialed HTTP,
    live/fixture dispatch for ``deploy_and_run``, and polling all come from
    ``VendorConnector`` (see ``session``, ``is_live``,
    ``_request``/``_request_json``/``_as_text``, ``_fixture_path``,
    ``_poll_until``); this class adds inventory fetching and this project's
    own AD-export fixture behavior.
    """

    #: Whether this vendor's credentials are shared across every client
    #: ("global" — one API token for the whole organization, e.g.
    #: SentinelOne) or distinct per client ("per_client", e.g. Carbon Black
    #: Cloud, where each environment has its own API ID/secret/org key). A
    #: real, fixed fact about how each vendor's API is provisioned — not
    #: something a config file should be able to override. Agent-parity's
    #: own concept, not part of the ``VendorConnector`` base (that
    #: class has no notion of multiple clients at all).
    scope: ClassVar[str] = "global"

    #: Tie-break priority among supports_remote_execution=True vendors when
    #: picking who carries a client's AD export (see
    #: agent_parity.config.pick_ad_export_vendor) — lower sorts first. Not
    #: just a technical preference: it reflects real deployment prevalence
    #: (SentinelOne covered the bulk of the original client base, Carbon
    #: Black a handful). The default leaves a new vendor sorting after both,
    #: alphabetically among any other default-priority vendors.
    ad_export_priority: ClassVar[int] = 100

    # -- inventory ---------------------------------------------------------

    def fetch_inventory(self) -> list[AgentDevice]:
        if self.is_live:
            return self._live_fetch_inventory()
        return self._fixture_fetch_inventory()

    def _fixture_fetch_inventory(self) -> list[AgentDevice]:
        # A labeled site/tenant (see AppConfig.sites_for) gets its own
        # fixture file — for a per_client vendor (Carbon Black) each tenant
        # really is a separate account, so one filtered file wouldn't be
        # honest; for a global vendor's site filter this just keeps the
        # demo data legible. Unlabeled (the common single-site/tenant case)
        # keeps today's plain per-vendor filename, unchanged.
        label = self.credentials.get("label")
        filename = f"{self.vendor}_inventory_{label}.json" if label else f"{self.vendor}_inventory.json"
        path = self._fixture_path(filename)
        with open(path) as fh:
            payload = json.load(fh)
        return rebase_timestamps(self._parse_inventory(payload))

    # -- remote script execution: this project's fixture behavior -----------

    def _fixture_deploy_and_run(self, script_path: Path, target_id: str, script_args: dict[str, str]) -> str:
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
    def _live_fetch_inventory(self) -> list[AgentDevice]: ...
