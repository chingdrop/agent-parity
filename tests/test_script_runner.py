"""Tests for the storage-backed vs. direct AD-export handoff paths.

Uses a hand-written fake connector (not a real vendor connector) to isolate
run_ad_export's orchestration logic — the connector implementations
themselves are already covered in test_connectors.py.
"""

from unittest.mock import Mock

import boto3
import pytest
import requests
from moto import mock_aws

from agent_parity.deployment.script_runner import ScriptExecutionError, run_ad_export
from agent_parity.storage import ObjectStorage

SAMPLE_CSV = "Name,Enabled\nACME-WS-001,True\n"


def _fake_connector(*, is_live: bool, deploy_and_run=None):
    connector = Mock()
    connector.vendor = "sentinelone"
    connector.is_live = is_live
    connector.deploy_and_run = deploy_and_run or Mock(return_value=SAMPLE_CSV)
    return connector


@pytest.fixture
def moto_storage():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield ObjectStorage(bucket="test-bucket", access_key="test", secret_key="test")


def test_no_storage_configured_uses_direct_channel():
    connector = _fake_connector(is_live=True)
    result = run_ad_export(connector, "ACME-DC01", storage=None)
    assert result == SAMPLE_CSV
    connector.deploy_and_run.assert_called_once()


def test_fixture_mode_never_touches_storage_even_if_configured():
    """A non-live connector has no real endpoint to upload anything from —
    the storage path must never engage regardless of whether storage is
    configured."""
    connector = _fake_connector(is_live=False)
    storage = Mock()

    result = run_ad_export(connector, "ACME-DC01", storage=storage)

    assert result == SAMPLE_CSV
    storage.presigned_put_url.assert_not_called()
    storage.get_object.assert_not_called()


def test_live_mode_with_storage_uploads_then_downloads(moto_storage):
    """The connector's return value is ignored entirely — the real output is
    whatever landed in object storage, simulating what Export-ADDevices.ps1
    actually does with the presigned URL it's handed."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        response = requests.put(script_args["UploadUrl"], data=SAMPLE_CSV.encode())
        response.raise_for_status()
        return "Uploaded 1 AD computer object(s) to object storage."

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))

    result = run_ad_export(connector, "ACME-DC01", storage=moto_storage)

    assert result == SAMPLE_CSV
    _, kwargs = connector.deploy_and_run.call_args
    assert "UploadUrl" in kwargs["script_args"]


def test_live_mode_with_storage_deletes_object_after_download(moto_storage):
    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=SAMPLE_CSV.encode()).raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    run_ad_export(connector, "ACME-DC01", storage=moto_storage, object_key="acme/export.csv")

    from agent_parity.storage import StorageError

    with pytest.raises(StorageError):
        moto_storage.get_object("acme/export.csv")


def test_empty_upload_is_rejected(moto_storage):
    """The script ran and uploaded *something*, but it's empty — still a
    failure, same as the direct-channel path returning nothing."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=b"").raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    with pytest.raises(ScriptExecutionError, match="returned no output"):
        run_ad_export(connector, "ACME-DC01", storage=moto_storage, object_key="acme/empty.csv")


def test_missing_upload_surfaces_as_storage_error(moto_storage):
    """A script that runs but never uploads anything (a real bug, e.g. a
    firewalled endpoint) shows up as a download failure, not a silent empty
    result — the object genuinely doesn't exist."""
    from agent_parity.storage import StorageError

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(return_value="ok"))
    with pytest.raises(StorageError):
        run_ad_export(connector, "ACME-DC01", storage=moto_storage, object_key="acme/never-uploaded.csv")
