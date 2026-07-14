"""SQLAlchemy schema + engine/session setup for run history and idempotency.

Historically this lived in a separate Django project (``agent_parity_web``)
that consumed ``agent_parity`` as a pinned dependency and owned scheduling/
persistence itself. That project (and its planned successor, a "hub" that
would have provided the same infrastructure without Django) is archived and
won't be developed further, so this package now owns that layer permanently
instead of provisionally — SQLAlchemy + SQLite rather than the Django ORM +
Postgres, but the same job: track one ``CorrelationRun`` per pipeline
execution and enough device/snapshot history to make a Celery chord callback
idempotent (see ``agent_parity.persistence``) and, later, to compute deltas
for Splunk export.

``config.yaml`` stays the sole topology/credential source — nothing here
duplicates client/vendor configuration. ``Client`` is just an identity
anchor for the foreign keys below, not a topology cache.

No Alembic: ``init_db()`` is a plain ``create_all``, sized for a
single-node/demo-scale run-history store, not a migration-managed production
schema. A consumer that outgrows this can layer Alembic on top of these same
models later.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from shared_tools.atomic_io import ensure_dir
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.types import JSON

DEFAULT_DB_URL = "sqlite:///agent_parity.db"


class RunStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class Base(DeclarativeBase):
    pass


class Client(Base):
    """One managed organization/environment — an identity anchor only.

    Topology (enabled vendors, AD domains, sync cadence) lives in
    ``config.yaml``/``ClientConfig``, not here; this row exists purely so
    ``Device``/``CorrelationRun`` have something to key off of.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    devices: Mapped[list[Device]] = relationship(back_populates="client")
    runs: Mapped[list[CorrelationRun]] = relationship(back_populates="client")


class Device(Base):
    """A device identity, keyed by the normalized hostname join key."""

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("client_id", "join_key", name="uniq_device_per_client"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    join_key: Mapped[str] = mapped_column(String(255), index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    os: Mapped[str] = mapped_column(String(255), default="")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client] = relationship(back_populates="devices")
    snapshots: Mapped[list[CoverageSnapshot]] = relationship(back_populates="device")


class CorrelationRun(Base):
    """One pipeline execution for one client."""

    __tablename__ = "correlation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.PENDING.value)
    # Config snapshot: what threshold this run was classified with.
    stale_days: Mapped[int] = mapped_column(Integer, default=14)
    # Per-source outcome, e.g. {"ad:ACME-DC01": "ok", "sentinelone": "ok",
    # "carbonblack:0": "error: ..."} — how partial runs stay honest.
    vendor_status: Mapped[dict] = mapped_column(JSON, default=dict)

    client: Mapped[Client] = relationship(back_populates="runs")
    snapshots: Mapped[list[CoverageSnapshot]] = relationship(back_populates="run")


class CoverageSnapshot(Base):
    """One device/vendor observation within one run."""

    __tablename__ = "coverage_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("correlation_runs.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    status: Mapped[str] = mapped_column(String(32), index=True)
    # Empty for missing_agent rows (no vendor observed the device).
    vendor: Mapped[str] = mapped_column(String(32), default="")
    match_method: Mapped[str] = mapped_column(String(32), default="")
    agent_last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Worded to match SentinelOne's own API vocabulary regardless of which
    # vendor actually reported the device (see AgentDevice's docstring in
    # agent_parity/models.py) — empty for missing_agent rows, same as vendor.
    platform: Mapped[str] = mapped_column(String(32), default="")
    machine_type: Mapped[str] = mapped_column(String(32), default="")
    # Always one of the four OSLifecycleStatus values, never blank — every
    # row gets a lifecycle classification, even "unknown" (see os_eol.py).
    eol_status: Mapped[str] = mapped_column(String(16), default="unknown")
    # The Windows build number that determined eol_status, when one was
    # available (AD or SentinelOne) — null when neither side had one.
    os_build: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped[CorrelationRun] = relationship(back_populates="snapshots")
    device: Mapped[Device] = relationship(back_populates="snapshots")


def get_engine(url: str | None = None) -> Engine:
    """Resolve the SQLAlchemy engine URL: explicit arg, else
    ``AGENT_PARITY_DB_URL``, else a local ``agent_parity.db`` SQLite file —
    same zero-setup default as ``sample_data/``'s fixture-mode fallback.
    """
    resolved = url or os.environ.get("AGENT_PARITY_DB_URL") or DEFAULT_DB_URL
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    if resolved.startswith("sqlite:///") and resolved != "sqlite:///:memory:":
        # A file-based URL (not in-memory) needs its parent directory to
        # exist before sqlite3 can create the file — e.g. AGENT_PARITY_DB_URL
        # pointed at a fresh Docker-volume path (see docker-compose.yml).
        db_path = Path(resolved.removeprefix("sqlite:///"))
        if str(db_path.parent) not in ("", "."):
            ensure_dir(db_path.parent)
    return create_engine(resolved, connect_args=connect_args)


def init_db(engine: Engine) -> None:
    """Create every table that doesn't already exist. Idempotent."""
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
