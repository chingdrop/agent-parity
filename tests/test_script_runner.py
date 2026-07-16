"""Tests for agent-parity's own storage-backed AD-export wrapper.

``run_ad_export``'s actual orchestration (live/fixture dispatch, the
presigned-URL round trip, validation) is
``shared_tools.script_export.run_script_export`` now, exhaustively tested in
``py-shared-tools``' own ``tests/test_script_export.py`` (mandatory-storage
rule, fixture mode never touching storage, upload/download/cleanup, empty
and wrong-shaped output). What's tested here is that this project's thin
wrapper supplies the right project-specific parameters — the AD-export
script path, the ``"ad-exports"`` object-key prefix, and the ``"Name"``
CSV-header sanity check — not a re-test of the shared logic's own branching.
"""

from unittest.mock import Mock

import boto3
import pytest
import requests
from moto import mock_aws
from shared_tools.storage import ObjectStorage

from agent_parity.script_runner import AD_EXPORT_SCRIPT, ScriptExecutionError, run_ad_export

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


def test_default_script_path_is_export_ad_devices():
    assert AD_EXPORT_SCRIPT.name == "Export-ADDevices.ps1"


def test_fixture_mode_wiring():
    connector = _fake_connector(is_live=False)
    assert run_ad_export(connector, "ACME-DC01", storage=None) == SAMPLE_CSV


def test_live_mode_wiring_round_trips_through_storage(moto_storage):
    """Proves run_ad_export threads object_key_prefix="ad-exports" and
    header_marker="Name" through to run_script_export correctly — the actual
    upload/download/cleanup mechanics are shared_tools' own to test."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=SAMPLE_CSV.encode()).raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    assert run_ad_export(connector, "ACME-DC01", storage=moto_storage) == SAMPLE_CSV


def test_live_mode_without_storage_raises_clear_error():
    connector = _fake_connector(is_live=True)
    with pytest.raises(ScriptExecutionError, match="object storage is required"):
        run_ad_export(connector, "ACME-DC01", storage=None)


def test_wrong_header_is_rejected_using_this_projects_marker(moto_storage):
    """ "Name" is this project's own header_marker — proves it's actually
    wired through, not left at run_script_export's generic default."""

    def fake_deploy_and_run(script_path, target_id, script_args=None):
        requests.put(script_args["UploadUrl"], data=b"NotTheRightHeader,Enabled\nx,y\n").raise_for_status()
        return "ok"

    connector = _fake_connector(is_live=True, deploy_and_run=Mock(side_effect=fake_deploy_and_run))
    with pytest.raises(ScriptExecutionError, match="does not look like"):
        run_ad_export(connector, "ACME-DC01", storage=moto_storage)
