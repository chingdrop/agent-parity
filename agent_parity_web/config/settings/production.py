"""Production settings: everything sensitive comes from the environment."""

from config.settings.base import *  # noqa: F401,F403

DEBUG = False

if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY must be set in production")

if not os.environ.get("CREDENTIAL_ENCRYPTION_KEY"):
    raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be set in production")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
