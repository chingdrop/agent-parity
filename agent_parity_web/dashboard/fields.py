"""An encryption-at-rest field for vendor credentials stored in the DB.

Deliberately ORM-layer (``cryptography.fernet``), not a Postgres-only
mechanism like ``pgcrypto`` — this app runs on SQLite in demo mode and
Postgres in scaled/Docker mode (``config/settings/base.py``), and the
encryption has to work identically on both.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


def _fernet() -> Fernet:
    key = settings.CREDENTIAL_ENCRYPTION_KEY
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedJSONField(models.TextField):
    """Stores an arbitrary JSON-serializable value, encrypted at rest.

    Used for ``VendorCredential.credentials`` — vendor credential shapes
    differ (SentinelOne's api_url/api_token vs Carbon Black's
    api_url/api_id/api_key/org_key), so this stays a single encrypted blob
    rather than per-vendor columns.
    """

    def get_prep_value(self, value):
        if value is None:
            return value
        plaintext = json.dumps(value)
        return _fernet().encrypt(plaintext.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return {}
        return json.loads(_fernet().decrypt(value.encode()).decode())

    def to_python(self, value):
        if value is None or isinstance(value, dict):
            return value
        try:
            return json.loads(_fernet().decrypt(value.encode()).decode())
        except Exception:
            return value
