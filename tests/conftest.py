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
