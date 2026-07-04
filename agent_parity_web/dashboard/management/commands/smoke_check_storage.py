"""Round-trip a real object through the configured object-storage backend.

``tests/test_storage.py`` proves ``ObjectStorage`` works against ``moto``'s
simulated S3 — this proves it against a real S3-compatible server (MinIO in
Docker Compose), including a real presigned-URL PUT over the actual network,
which moto can only approximate. Used by ``docker/smoke_test.sh``, not part
of the demo or production flow — the demo path never has storage configured
at all (see ``agent_parity/storage.py``'s module docstring).
"""

import uuid

import requests
from botocore.exceptions import ClientError
from django.core.management.base import BaseCommand, CommandError

from agent_parity.config import get_storage
from agent_parity.storage import StorageError
from dashboard.config_db import build_app_config_from_db


class Command(BaseCommand):
    help = "Smoke test only: round-trip a real object through object storage."

    def handle(self, *args, **options):
        config = build_app_config_from_db()
        storage = get_storage(config)
        if storage is None:
            raise CommandError(
                "object storage is not configured (STORAGE_BUCKET/STORAGE_ACCESS_KEY/"
                "STORAGE_SECRET_KEY) — nothing to smoke test. docker/smoke_test.sh sets "
                "these explicitly for this command; check how it's invoked."
            )

        self._ensure_bucket(storage)

        key = f"smoke-test/{uuid.uuid4().hex}.csv"
        body = b"Name,Enabled\nSMOKE-TEST,True\n"

        try:
            url = storage.presigned_put_url(key)
            response = requests.put(url, data=body, timeout=30)
            response.raise_for_status()

            downloaded = storage.get_object(key)
            if downloaded != body:
                raise CommandError(f"round-trip mismatch: expected {body!r}, got {downloaded!r}")

            storage.delete_object(key)
        except StorageError as exc:
            raise CommandError(f"object storage round trip failed: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(f"object storage round trip OK ({storage.bucket}/{key})")
        )

    @staticmethod
    def _ensure_bucket(storage) -> None:
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
