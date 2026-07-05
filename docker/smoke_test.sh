#!/usr/bin/env bash
# MinIO integration smoke test.
#
# Proves the one thing the fast, offline `uv run pytest` suite structurally
# can't: agent_parity/storage.py round-trips a real object (including a real
# presigned-URL PUT over the actual network) through a real S3-compatible
# server, not moto's simulation. Needs Docker; not part of `uv run pytest` or
# any fast/CI path — run manually, e.g. before a release.
#
# Usage: docker/smoke_test.sh [--keep]
#   --keep   leave MinIO running on exit (default: always tears down)

set -uo pipefail
# shellcheck disable=SC2164
cd "$(dirname "$0")"

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

# MinIO's own root credentials — read from .env if present so the smoke
# test can't drift from whatever the stack is actually configured with;
# fall back to docker-compose.yml's own defaults otherwise.
if [[ -f ../.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source ../.env
    set +a
fi
MINIO_ROOT_USER="${MINIO_ROOT_USER:-agent_parity}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-agent_parity_minio}"

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        echo "--- --keep passed: leaving MinIO running ---"
        return
    fi
    echo "--- tearing down ---"
    docker compose down -v --remove-orphans
}
trap cleanup EXIT

echo "--- starting MinIO ---"
if ! docker compose up -d --wait minio; then
    echo "FAIL: docker compose up" >&2
    exit 1
fi

echo "--- round-tripping a real object through MinIO (not moto) ---"
if STORAGE_ENDPOINT_URL=http://localhost:9000 \
    STORAGE_BUCKET=smoke-test \
    STORAGE_ACCESS_KEY="$MINIO_ROOT_USER" \
    STORAGE_SECRET_KEY="$MINIO_ROOT_PASSWORD" \
    uv run python smoke_check_storage.py; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== SMOKE TEST FAILED ===" >&2
    exit 1
fi
