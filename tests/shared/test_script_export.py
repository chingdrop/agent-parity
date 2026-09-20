"""Tests for the storage-backed script-export handoff and the
mandatory-storage rule for live connectors.

This is the union of agent-parity's and credential-audit's own
test_script_runner.py coverage (both projects' versions were near-identical
before this moved here) — including the wrong-shaped-output checks that only
credential-audit's copy had, now covered for every consumer. Uses a
hand-written fake connector (not a real vendor connector) to isolate
run_script_export's orchestration logic.
"""

from unittest.mock import Mock

import boto3
import pytest
import requests
from moto import mock_aws

from agent_parity.shared.script_export import ScriptExecutionError, run_script_export
from agent_parity.shared.storage import ObjectStorage

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


def test_live_connector_without_storage_raises_clear_error():
    """Storage is mandatory for a live export — the vendor's own
    remote-execution output channel doesn't reliably preserve the CSV's
    formatting, so a missing storage config must never silently fall back
    to it."""
    connector = _fake_connector(is_live=True)
    with pytest.raises(ScriptExecutionError, match="object storage is required"):
        run_script_export(connector, "ACME-DC01", "script.ps1", storage=None)
    connector.deploy_and_run.assert_not_called()


def test_fixture_mode_never_touches_storage_even_if_configured():
    """A non-live connector has no real endpoint to upload anything from —
    the storage path must never engage regardless of whether storage is
    configured."""
    connector = _fake_connector(is_live=False)
    storage = Mock()

    result = run_script_export(connector, "ACME-DC01", "script.ps1", storage=storage)

    assert result == SAMPLE_CSV
    storage.presigned_put_url.assert_not_called()
    storage.get_object.assert_not_called()


def test_live_mode_with_storage_uploads_then_downloads(moto_storage):
    """The connector's return value is ignored entirely — the real output is
    whatever landed in object storage, simulating what the pushed script
    actually does with the presigned URL it's handed."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        response = requests.put(script_args["UploadUrl"], data=SAMPLE_CSV.encode(), timeout=10)
        response.raise_for_status()
        return "Uploaded to object storage."

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))

    result = run_script_export(connector, "ACME-DC01", "script.ps1", storage=moto_storage)

    assert result == SAMPLE_CSV
    _, kwargs = connector.deploy_and_run.call_args
    assert "UploadUrl" in kwargs["script_args"]


def test_live_mode_with_storage_deletes_object_after_download(moto_storage):
    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=SAMPLE_CSV.encode(), timeout=10).raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    run_script_export(connector, "ACME-DC01", "script.ps1", storage=moto_storage, object_key="acme/export.csv")

    from agent_parity.shared.storage import StorageError

    with pytest.raises(StorageError):
        moto_storage.get_object("acme/export.csv")


def test_empty_upload_is_rejected(moto_storage):
    """The script ran and uploaded *something*, but it's empty — still a
    failure, same as the direct-channel path returning nothing."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=b"", timeout=10).raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    with pytest.raises(ScriptExecutionError, match="returned no output"):
        run_script_export(connector, "ACME-DC01", "script.ps1", storage=moto_storage, object_key="acme/empty.csv")


def test_missing_upload_surfaces_as_storage_error(moto_storage):
    """A script that runs but never uploads anything (a real bug, e.g. a
    firewalled endpoint) shows up as a download failure, not a silent empty
    result — the object genuinely doesn't exist."""
    from agent_parity.shared.storage import StorageError

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(return_value="ok"))
    with pytest.raises(StorageError):
        run_script_export(
            connector,
            "ACME-DC01",
            "script.ps1",
            storage=moto_storage,
            object_key="acme/never-uploaded.csv",
        )


def test_wrong_shaped_output_is_rejected(moto_storage):
    """An upload that doesn't match the expected header_marker is rejected
    rather than silently returned to the caller."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=b"not,the,right,csv\n", timeout=10).raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    with pytest.raises(ScriptExecutionError, match="does not look like"):
        run_script_export(connector, "ACME-DC01", "script.ps1", storage=moto_storage, object_key="acme/wrong.csv")


def test_fixture_mode_rejects_wrong_shaped_output():
    """The output-shape sanity check applies to the fixture-mode path too,
    not just the storage-backed live path."""
    connector = _fake_connector(is_live=False, deploy_and_run=Mock(return_value="not the right csv\n"))
    with pytest.raises(ScriptExecutionError, match="does not look like"):
        run_script_export(connector, "ACME-DC01", "script.ps1", storage=None)


def test_object_key_prefix_is_used_when_no_explicit_key():
    """object_key_prefix scopes objects within a shared bucket — two projects
    pointed at the same bucket must never collide."""
    storage = Mock()
    storage.get_object.return_value = SAMPLE_CSV.encode()
    connector = _fake_connector(is_live=True)

    run_script_export(connector, "ACME-DC01", "script.ps1", storage=storage, object_key_prefix="ad-metadata")

    (put_key,), _ = storage.presigned_put_url.call_args
    (get_key,), _ = storage.get_object.call_args
    (delete_key,), _ = storage.delete_object.call_args
    assert put_key == get_key == delete_key
    assert put_key.startswith("ad-metadata/sentinelone/")
    assert put_key.endswith(".csv")


def test_header_marker_customizes_validation(moto_storage):
    """A caller-chosen header_marker (e.g. "sAMAccountName") is what gets
    checked, not a hardcoded default."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(
            script_args["UploadUrl"], data=b"sAMAccountName,PasswordLastSet\njdoe,2020-01-01\n", timeout=10
        ).raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    result = run_script_export(
        connector,
        "ACME-DC01",
        "script.ps1",
        storage=moto_storage,
        header_marker="sAMAccountName",
    )
    assert result.startswith("sAMAccountName,")


def test_what_customizes_error_wording():
    connector = _fake_connector(is_live=True)
    with pytest.raises(ScriptExecutionError, match="live AD metadata export"):
        run_script_export(connector, "ACME-DC01", "script.ps1", storage=None, what="AD metadata export")
