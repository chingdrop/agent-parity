"""Round-trip a real object through a real S3-compatible server (MinIO).

``tests/test_storage.py`` proves ``ObjectStorage`` works against ``moto``'s
simulated S3 — this proves it against the genuine article, including a real
presigned-URL PUT over the actual network, which moto can only approximate.
Run manually via ``docker/smoke_test.sh``; not part of ``uv run pytest``.

Usage: STORAGE_BUCKET=... STORAGE_ACCESS_KEY=... STORAGE_SECRET_KEY=...
       STORAGE_ENDPOINT_URL=http://localhost:9000 uv run python docker/smoke_check_storage.py
"""

from __future__ import annotations

import os
import sys
import uuid

import requests
from botocore.exceptions import ClientError

from agent_parity.storage import ObjectStorage, StorageError


def _ensure_bucket(storage: ObjectStorage) -> None:
    """Create the smoke-test bucket if it doesn't exist yet.

    MinIO doesn't auto-create buckets; production use is expected to
    provision the real bucket out-of-band (Terraform, the AWS/MinIO
    console, ...), so ``ObjectStorage`` itself deliberately has no
    bucket-administration methods — this is smoke-test-only setup.
    """
    try:
        storage.client.create_bucket(Bucket=storage.bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise


def main() -> int:
    bucket = os.environ.get("STORAGE_BUCKET")
    access_key = os.environ.get("STORAGE_ACCESS_KEY")
    secret_key = os.environ.get("STORAGE_SECRET_KEY")
    if not (bucket and access_key and secret_key):
        print(
            "object storage is not configured (STORAGE_BUCKET/STORAGE_ACCESS_KEY/"
            "STORAGE_SECRET_KEY) — nothing to smoke test.",
            file=sys.stderr,
        )
        return 1

    storage = ObjectStorage(
        bucket=bucket,
        endpoint_url=os.environ.get("STORAGE_ENDPOINT_URL") or None,
        access_key=access_key,
        secret_key=secret_key,
        region=os.environ.get("STORAGE_REGION", "us-east-1"),
    )
    _ensure_bucket(storage)

    key = f"smoke-test/{uuid.uuid4().hex}.csv"
    body = b"Name,Enabled\nSMOKE-TEST,True\n"

    try:
        url = storage.presigned_put_url(key)
        response = requests.put(url, data=body, timeout=30)
        response.raise_for_status()

        downloaded = storage.get_object(key)
        if downloaded != body:
            print(f"round-trip mismatch: expected {body!r}, got {downloaded!r}", file=sys.stderr)
            return 1

        storage.delete_object(key)
    except StorageError as exc:
        print(f"object storage round trip failed: {exc}", file=sys.stderr)
        return 1

    print(f"object storage round trip OK ({storage.bucket}/{key})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
