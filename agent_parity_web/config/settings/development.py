"""Development settings: DEBUG on, permissive hosts, plain static storage."""

from config.settings.base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Manifest static storage requires collectstatic; not wanted in dev.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}
