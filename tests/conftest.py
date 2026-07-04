import pytest


@pytest.fixture
def eager_celery():
    """Run Celery tasks synchronously in-process for the duration of a test."""
    from config import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield celery_app
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


@pytest.fixture
def db_config(db):
    """An AppConfig sourced from the DB, seeded from the real config.yaml.

    Production entrypoints build their config via
    ``dashboard.config_db.build_app_config_from_db()`` now, not
    ``agent_parity.config.load_config()`` directly — tests that exercise
    those entrypoints (Celery tasks, management commands) need equivalent
    Client/VendorCredential rows to exist, not just an in-memory AppConfig.
    This imports the repo's real config.yaml (acme/globex, no live
    credentials in the test environment — same fixture-mode fallback as
    every other test) once per test and returns the resulting DB-backed
    AppConfig.
    """
    from agent_parity.config import load_config
    from dashboard.config_db import build_app_config_from_db, import_app_config

    import_app_config(load_config())
    return build_app_config_from_db()
