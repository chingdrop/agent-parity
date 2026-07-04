"""S3-compatible object storage for the AD-export handoff.

Vendor remote-execution channels (SentinelOne Remote Script Orchestration,
Carbon Black Live Response) have real output-size limits — fine for a
management-console command, not necessarily fine for a full AD computer
export from a large environment. Rather than squeeze the CSV through that
channel, the remote script can instead upload it directly to object storage
via a short-lived, single-object presigned PUT URL: the endpoint never holds
a standing storage credential, only a URL that expires in minutes and can
write exactly one key. agent-parity then downloads it with its own
credentials — a strictly narrower trust footprint than either a shared FTP
login or routing the payload through the vendor API at all.

This is deliberately built against the S3 API (via boto3), not a specific
product: point ``endpoint_url`` at a self-hosted MinIO instance for local/dev
use (the Docker Compose stack runs one), or leave it unset to talk to real
AWS S3 in production — same class, same code, just different config. This is
*not* Azure Blob Storage capable: Azure Blob doesn't speak the S3 API, so
supporting it would mean a second implementation with a different SDK
(``azure-storage-blob``), not just different credentials on this one.
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
    """Thin wrapper around a boto3 S3 client for the AD-export handoff.

    Every method wraps botocore's exceptions in ``StorageError``, the same
    convention connectors use for ``ConnectorError`` — callers only need to
    catch one exception type regardless of what boto3 raises underneath.
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
        rejected — an easy, unnecessary footgun for a plain PowerShell
        ``Invoke-RestMethod -Method Put`` call.
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
        """Best-effort cleanup after a successful download.

        S3-compatible delete is already idempotent (deleting a missing key
        isn't an error), so this only ever guards against genuine failures
        (permissions, network) — which must never fail the AD export that
        already succeeded.
        """
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("failed to delete %r after AD export handoff: %s", key, exc)
