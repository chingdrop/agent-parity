# 0011. Versity S3 Gateway for local development, replacing MinIO

Status: Accepted (2026-09-28). Replaces the local-server choice in [0003](0003-s3-api-with-minio-for-local-dev.md); the
rest of 0003 (build on the S3 API, AWS S3 in production) stands.

## Context

[0003](0003-s3-api-with-minio-for-local-dev.md) picked MinIO as the local S3 server for Docker Compose. MinIO stopped
publishing community images in October 2025 and archived the project in April 2026, and in September 2026 Docker Hub
removed the `minio` namespace. `minio/minio:latest` stopped pulling, which broke `docker/smoke_test.sh` and any compose
command that started the `agent-parity` service with its dependencies.

The project needs very little from this server. It is only for local development and the manual smoke test, production
uses AWS S3, and the code only uses presigned PUT, create bucket, get and delete.

## Decision

The compose `s3` service runs the [Versity S3 Gateway](https://github.com/versity/versitygw) (`versity/versitygw`),
pinned to a version tag, serving a local directory through its `posix` backend. The service and its variables are named
for the S3 API, not the product (`s3`, `s3data`, `LOCAL_S3_ACCESS_KEY`/`LOCAL_S3_SECRET_KEY`), and the healthcheck uses
the gateway's own unauthenticated `--health` endpoint.

Each candidate was run against `docker/smoke_check_storage.py` (bucket creation and a presigned-PUT round trip), and
checked to reject an unsigned PUT and a presigned URL signed with the wrong secret with 403. Versity passed, including
a PUT with `Content-Type: text/csv` the way `Export-ADDevices.ps1` sends it.

## Alternatives considered

- **Chainguard's MinIO image** (`cgr.dev/chainguard/minio`): passed, and was a one-line change, but only a floating
  `latest` tag was confirmed free, and it rebuilds an archived project.
- **SeaweedFS**: passed and is mature, but it is a distributed storage system at about seven times the image size, more
  than a single demo bucket needs.
- **RustFS**: passed, but was still an alpha release.
- **Garage**: AGPL, and needs a config file plus cluster-layout and key setup before it serves anything.

## Consequences

- No Python code changed: `ObjectStorage` only knows an `endpoint_url`, which is what 0003 intended.
- The image is pinned, so the stack no longer changes or breaks when a floating tag moves or disappears.
- `.env` files using `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` need renaming to `LOCAL_S3_ACCESS_KEY`/
  `LOCAL_S3_SECRET_KEY`.
- There is no web console (MinIO's port 9001); the override file only publishes the S3 port.
- Versity has a smaller community than SeaweedFS, which stays the fallback if the gateway ever causes trouble.

## Evidence

- Code: [`docker/docker-compose.yml`](../../docker/docker-compose.yml),
  [`docker/docker-compose.override.yml`](../../docker/docker-compose.override.yml),
  [`docker/smoke_test.sh`](../../docker/smoke_test.sh), [`docker/smoke_check_storage.py`](../../docker/smoke_check_storage.py)
- Verified by running `docker/smoke_test.sh` end to end against the new stack.
