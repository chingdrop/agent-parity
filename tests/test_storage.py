"""Object storage tests: presigned-URL round trip against a mocked S3 backend
(moto) — no real MinIO or AWS S3 needed, no real network access.
"""

import boto3
import pytest
import requests
from moto import mock_aws

from agent_parity.storage import ObjectStorage, StorageError


@pytest.fixture
def storage():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield ObjectStorage(
            bucket="test-bucket", access_key="test", secret_key="test", region="us-east-1"
        )


def test_presigned_put_url_round_trips_content(storage):
    """Simulates exactly what Export-ADDevices.ps1 does: PUT to the presigned
    URL, then a plain GET on the agent-parity side."""
    url = storage.presigned_put_url("acme/ad_export.csv")

    response = requests.put(url, data=b"Name,Enabled\nACME-WS-001,True\n")
    response.raise_for_status()

    assert storage.get_object("acme/ad_export.csv") == b"Name,Enabled\nACME-WS-001,True\n"


def test_presigned_url_expires_quickly_by_default(storage):
    url = storage.presigned_put_url("x.csv", expires_in=900)
    assert "X-Amz-Expires=900" in url


def test_get_object_missing_key_raises_storage_error(storage):
    with pytest.raises(StorageError, match="failed to download"):
        storage.get_object("does/not/exist.csv")


def test_delete_object_missing_key_does_not_raise(storage):
    # S3 delete is idempotent — deleting a key that was never uploaded is not
    # an error, so this should complete silently rather than raise.
    storage.delete_object("never-uploaded.csv")


def test_delete_object_swallows_client_errors(storage, monkeypatch, caplog):
    """Cleanup must never fail an AD export that already succeeded."""
    from botocore.exceptions import ClientError

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DeleteObject")

    monkeypatch.setattr(storage.client, "delete_object", boom)
    storage.delete_object("some-key.csv")  # must not raise
    assert "failed to delete" in caplog.text
