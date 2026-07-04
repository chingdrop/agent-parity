"""S3-compatible object storage — a small, dependency-minimal wrapper around
boto3 built around one specific pattern: hand an untrusted or low-trust
caller a short-lived, single-object presigned PUT URL instead of a standing
storage credential, then fetch/clean up the result with your own real
credentials. Reused across projects (this file is meant to be copied
as-is, the same way ``rest_adapter.py`` in this repo was); only ``boto3``
and stdlib are required.

In agent-parity specifically: vendor remote-execution channels (SentinelOne
Remote Script Orchestration, Carbon Black Live Response) have real
output-size limits — fine for a management-console command, not necessarily
fine for a full AD computer export from a large environment. Rather than
squeeze the CSV through that channel, the remote script uploads it directly
via a presigned PUT URL instead: the endpoint never holds a standing storage
credential, only a URL that expires in minutes and can write exactly one
key — a strictly narrower trust footprint than either a shared FTP login or
routing the payload through the vendor API at all.

Built against the S3 API, not a specific product: point ``endpoint_url`` at a
self-hosted MinIO instance for local/dev use, or leave it unset to talk to
real AWS S3 in production — same class, same code, just different config.
This is *not* Azure Blob Storage capable: Azure Blob doesn't speak the S3
API, so supporting it would mean a second implementation with a different
SDK (``azure-storage-blob``), not just different credentials on this one.
"""

from __future__ import annotations

import logging

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """An object-storage operation failed."""


class ObjectStorage:
    """Thin wrapper around a boto3 S3 client: presigned uploads, downloads,
    best-effort cleanup.

    Every method wraps botocore's exceptions in ``StorageError`` — callers
    only need to catch one exception type regardless of what boto3 raises
    underneath.
    """

    def __init__(
            self,
            bucket: str,
            *,
            endpoint_url: str | None = None,
            access_key: str | None = None,
            secret_key: str | None = None,
            region: str = "us-east-1",
    ):
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=BotoConfig(
                # MinIO (and most non-AWS S3-compatible services) expect
                # path-style bucket addressing (http://host/bucket/key)
                # rather than AWS's virtual-hosted-style (http://bucket.host/key).
                s3={"addressing_style": "path"},
                # Explicit rather than relying on boto3's default: SigV2 is
                # deprecated on real AWS S3 and unsupported by some
                # S3-compatible services entirely, so pin the modern scheme.
                signature_version="s3v4",
            ),
        )

    def presigned_put_url(self, key: str, expires_in: int = 900) -> str:
        """A short-lived URL that can PUT exactly one object, nothing else.

        Deliberately doesn't bind a Content-Type: doing so requires the
        uploader's request to match it exactly, or the signature is
        rejected — an easy, unnecessary footgun for a caller that just wants
        to PUT raw bytes (a plain PowerShell ``Invoke-RestMethod -Method
        Put``, a browser upload, a curl call — whatever's on the other end).
        """
        try:
            return self.client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(
                f"failed to create presigned upload URL for {key!r}: {exc}"
            ) from exc

    def get_object(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"failed to download {key!r}: {exc}") from exc

    def delete_object(self, key: str) -> None:
        """Best-effort cleanup, typically called after a successful download.

        S3-compatible delete is already idempotent (deleting a missing key
        isn't an error), so this only ever guards against genuine failures
        (permissions, network) — logged, not raised, since cleanup failing
        must never fail whatever operation already succeeded.
        """
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("failed to delete %r: %s", key, exc)
