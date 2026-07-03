# Import the Celery app when Django starts so @shared_task decorators bind to
# it — the standard Django+Celery wiring.
from config.celery import app as celery_app

__all__ = ("celery_app",)
