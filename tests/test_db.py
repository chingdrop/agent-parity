"""SQLAlchemy schema tests: model round-trip and constraint enforcement.

Everything runs against an in-memory SQLite engine — no file, no fixtures.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from agent_parity.scheduling.db import (
    Client,
    CorrelationRun,
    CoverageSnapshot,
    Device,
    RunStatus,
    get_engine,
    init_db,
    session_factory,
)


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_client_run_device_snapshot_round_trip(session):
    client = Client(slug="acme", name="Acme Corp")
    session.add(client)
    session.flush()

    run = CorrelationRun(client_id=client.id, stale_days=14)
    session.add(run)
    session.flush()
    assert run.status == RunStatus.PENDING.value

    device = Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001", os="Windows 11")
    session.add(device)
    session.flush()

    snapshot = CoverageSnapshot(run_id=run.id, device_id=device.id, status="covered", vendor="sentinelone")
    session.add(snapshot)
    session.commit()

    assert client.runs == [run]
    assert client.devices == [device]
    assert run.snapshots == [snapshot]
    assert device.snapshots == [snapshot]


def test_device_unique_constraint_per_client_join_key(session):
    client = Client(slug="acme", name="Acme Corp")
    session.add(client)
    session.flush()

    session.add(Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001"))
    session.flush()
    session.add(Device(client_id=client.id, join_key="acme-ws-001", hostname="ACME-WS-001-DUP"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_join_key_allowed_across_different_clients(session):
    acme = Client(slug="acme", name="Acme Corp")
    globex = Client(slug="globex", name="Globex")
    session.add_all([acme, globex])
    session.flush()

    session.add(Device(client_id=acme.id, join_key="dc01", hostname="ACME-DC01"))
    session.add(Device(client_id=globex.id, join_key="dc01", hostname="GLOBEX-DC01"))
    session.flush()  # no IntegrityError: uniqueness is scoped per client
