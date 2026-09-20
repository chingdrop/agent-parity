# 0003. Build on the S3 API (boto3), with MinIO for local development

Status: Accepted (2026-07-03)

## Context

The presigned-URL handoff in [0002](0002-return-ad-export-via-presigned-put-url.md) needs an object store that can issue
and honor presigned URLs, and that can also be run locally for development.
<!-- TODO(craig): why the S3 API was chosen over other options. The repo says only that the code targets "the S3 API, not a specific product". -->

## Decision

`ObjectStorage` wraps boto3's S3 client. Only `endpoint_url` changes between a self-hosted MinIO instance (local and
dev, via `docker/docker-compose.yml`) and real AWS S3. `StorageConfig.backend` supports only `"s3"`.

## Alternatives considered

- **Azure Blob Storage**: explicitly not supported. It doesn't speak the S3 API, so it would need a second
  implementation with a different SDK (`azure-storage-blob`), not just different credentials.
- <!-- TODO(craig): other storage backends you considered. -->

## Consequences

- `get_storage` raises `ConfigError` for any backend other than `"s3"`.
- Path-style addressing and SigV4 are pinned for S3-compatible services.
- `ObjectStorage` has no bucket-admin methods on purpose; bucket provisioning is out-of-band, and only the smoke test
  creates a bucket.
- Tests use `moto`; `docker/smoke_check_storage.py` exercises real MinIO and is not part of `uv run pytest`.
- Rules out Azure Blob without new code.

## Evidence

- Code: [`src/agent_parity/shared/storage.py`](../../src/agent_parity/shared/storage.py), `get_storage` in [
  `src/agent_parity/shared/config.py`](../../src/agent_parity/shared/config.py), [
  `docker/docker-compose.yml`](../../docker/docker-compose.yml), [
  `docker/smoke_check_storage.py`](../../docker/smoke_check_storage.py)
- Tests: [`tests/shared/test_config.py`](../../tests/shared/test_config.py)
  `test_get_storage_rejects_unsupported_backend`; [`tests/test_config.py`](../../tests/test_config.py)
  `test_storage_rejects_unsupported_backend`
