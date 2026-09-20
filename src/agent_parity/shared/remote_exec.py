"""Generic vendor-API "connector" base: credentialed HTTP plus an optional
push-a-script-and-run-it-remotely capability (SentinelOne's Remote Script
Orchestration, Carbon Black's Live Response, ...) — shared across projects
that talk to the same kind of EDR/security-console vendor APIs.

Live/fixture mode is a first-class fork here, not a test-only shim: when a
connector has no usable credentials (``is_live`` is false), every public
method is expected to fall back to canned fixture data instead of raising —
that's what lets a fresh checkout run a full pipeline with zero live API
access. What "canned data" actually looks like (file naming, any
post-processing) is project-specific, so it is delegated to hook methods each
consumer's own subclass implements — this module only owns the parts that are
identical regardless of what a connector fetches: credential-gated dispatch,
the shared ``RestAdapter`` session, polling, and the vendor registry.

Not every vendor's real API exposes "run an arbitrary script on a managed
endpoint" — subclasses set ``supports_remote_execution = False`` when it
doesn't; ``deploy_and_run`` refuses outright in that case, in both live and
fixture mode, so a consuming pipeline can't accidentally attribute a
capability to a vendor that doesn't really have it.
"""

from __future__ import annotations

import logging
import time
from abc import ABC
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import requests

from agent_parity.shared.rest_adapter import RestAdapter, RestAdapterConfig


class ConnectorError(Exception):
    """A vendor API call, remote execution, or fixture lookup failed."""


class ConnectorRegistry(dict):
    """vendor-name -> connector class, keyed by each registered class's own
    ``vendor`` attribute.

    Each consuming project owns one instance (e.g. agent-parity's
    ``CONNECTOR_REGISTRY = ConnectorRegistry()``) rather than sharing a single
    process-wide dict, so unrelated projects' vendor connectors never collide
    just because they both build on this base.
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
    ``_live_*``/fixture hook methods their own domain needs (e.g. a project
    that fetches inventories adds its own ``fetch_inventory``); this class
    only provides what every vendor connector needs regardless of what it
    fetches: the shared ``RestAdapter`` session, credential-gated live/fixture
    dispatch, and — for vendors that support it — remote script execution.
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
