import pytest


@pytest.fixture
def celery_eager():
    """Run Celery tasks synchronously in-process for the duration of a test —
    no broker needed. The semantics under test (results-tolerant callback,
    pre-created run id as idempotency key) are identical either way."""
    from agent_parity.scheduling.celery_app import app

    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    yield app
    app.conf.task_always_eager = False
    app.conf.task_eager_propagates = False


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """Point AGENT_PARITY_DB_URL at a fresh tmp_path file for the duration of
    a test. A real file (not sqlite:///:memory:) is required here because
    agent_parity.scheduling.tasks opens a brand new engine/connection per task — an
    in-memory database wouldn't persist across those separate connections."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("AGENT_PARITY_DB_URL", f"sqlite:///{db_path}")
    return f"sqlite:///{db_path}"
