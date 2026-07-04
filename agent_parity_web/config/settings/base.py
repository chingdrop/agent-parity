"""Shared Django settings. development.py / production.py override the rest."""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from celery.schedules import crontab

# agent_parity_web/ — the Django project root.
BASE_DIR = Path(__file__).resolve().parents[2]
# Repo root, where the agent_parity core package and sample_data/ live.
REPO_ROOT = BASE_DIR.parent

# Celery workers and WSGI servers start with agent_parity_web/ as their
# import root; the core pipeline package lives one level up.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-dev-only-key-change-me")

DEBUG = False
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-level templates (base.html, shared layout); app templates
        # under dashboard/templates/ extend these via APP_DIRS.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


def _database_from_env() -> dict:
    """SQLite by default (demo mode); DATABASE_URL (postgres) when provided."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": REPO_ROOT / "db.sqlite3",
        }
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or 5432),
    }


DATABASES = {"default": _database_from_env()}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Celery (CELERY_ namespace, consumed by config/celery.py) ---------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# Beat ticks the orchestrator hourly; per-client cadence (sync_interval_hours
# in config.yaml) decides whether each client is actually due. A second,
# daily tick at 07:00 (CELERY_TIMEZONE, i.e. UTC) force-dispatches every
# active client regardless of its own interval, so a fresh correlation is
# always ready before the start of business each morning.
CELERY_BEAT_SCHEDULE = {
    "sync-all-clients": {
        "task": "dashboard.tasks.dispatch_all_clients",
        "schedule": 60 * 60,
    },
    "sync-all-clients-7am": {
        "task": "dashboard.tasks.dispatch_all_clients",
        "schedule": crontab(hour=7, minute=0),
        "kwargs": {"force": True},
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
