"""Tests for dashboard/fields.py's EncryptedJSONField in isolation — the
encrypt/decrypt mechanics, independent of the VendorCredential model (which
tests/test_dashboard_models.py covers for the DB round-trip and uniqueness
constraints).
"""

from dashboard.fields import EncryptedJSONField


def test_get_prep_value_returns_none_for_none():
    field = EncryptedJSONField()
    assert field.get_prep_value(None) is None


def test_get_prep_value_encrypts_to_something_other_than_plaintext_json():
    field = EncryptedJSONField()
    stored = field.get_prep_value({"api_token": "s1-secret"})
    assert "s1-secret" not in stored
    assert "api_token" not in stored


def test_round_trip_through_get_prep_value_and_from_db_value():
    field = EncryptedJSONField()
    creds = {"api_url": "https://usea1.sentinelone.net", "api_token": "s1-secret"}
    stored = field.get_prep_value(creds)
    assert field.from_db_value(stored, None, None) == creds


def test_from_db_value_returns_empty_dict_for_none_or_blank():
    field = EncryptedJSONField()
    assert field.from_db_value(None, None, None) == {}
    assert field.from_db_value("", None, None) == {}


def test_encrypting_the_same_value_twice_produces_different_ciphertext():
    """Fernet includes a random IV per call — this is what makes the
    ciphertext non-deterministic even for identical plaintext, so two
    VendorCredential rows with the same secret don't reveal that fact from
    the stored bytes alone."""
    field = EncryptedJSONField()
    creds = {"api_key": "same-secret"}
    assert field.get_prep_value(creds) != field.get_prep_value(creds)


def test_to_python_passes_through_a_dict_unchanged():
    field = EncryptedJSONField()
    creds = {"api_key": "x"}
    assert field.to_python(creds) is creds
