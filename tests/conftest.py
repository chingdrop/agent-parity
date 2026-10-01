import pytest
import requests


@pytest.fixture(autouse=True)
def offline_os_eol(tmp_path, monkeypatch):
    """No test reaches endoflife.date, and none reads or writes a real EOL cache.

    `agent-parity run` refreshes the OS EOL data at start; here that fetch fails
    the way an offline machine's would, so runs use the bundled snapshot. Tests
    of the refresh itself patch the fetch over this.
    """
    monkeypatch.setenv("AGENT_PARITY_EOL_CACHE", str(tmp_path / "os_eol_cache.json"))

    def offline(url, timeout):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr("agent_parity.os_eol_live.requests.get", offline)


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
