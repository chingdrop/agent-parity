#!/usr/bin/env bash
# Object storage + Celery integration smoke test.
#
# Proves two things the fast, offline `uv run pytest` suite structurally
# can't: agent_parity.storage.ObjectStorage round-trips a real object
# (including a real presigned-URL PUT over the actual network) through a
# real S3-compatible server, not moto's simulation; and a real Celery chord
# fans out/in through a real Redis broker and real worker/beat containers,
# not task_always_eager. Needs Docker, so it isn't part of `uv run pytest`;
# .github/workflows/smoke.yml runs it weekly and on changes to the stack.
#
# Usage: docker/smoke_test.sh [--keep]
#   --keep   leave the stack running on exit (default: always tears down)

set -uo pipefail
# shellcheck disable=SC2164
cd "$(dirname "$0")"

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

# The local S3 server's root credentials — read from .env if present so the smoke
# test can't drift from whatever the stack is actually configured with;
# fall back to docker-compose.yml's own defaults otherwise.
if [[ -f ../.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source ../.env
    set +a
fi
LOCAL_S3_ACCESS_KEY="${LOCAL_S3_ACCESS_KEY:-agent_parity}"
LOCAL_S3_SECRET_KEY="${LOCAL_S3_SECRET_KEY:-agent_parity_s3}"

# Always its own compose project (set after .env is sourced, so nothing there
# can override it): the teardown's `down -v` then only removes this test's own
# containers and volumes, never a dev stack's run history (docker_dbdata). It
# still publishes port 9000, so stop a dev stack's `s3` service first if one is up.
export COMPOSE_PROJECT_NAME=agent-parity-smoke

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        echo "--- --keep passed: leaving the stack running ---"
        return
    fi
    echo "--- tearing down ---"
    docker compose down -v --remove-orphans
}
trap cleanup EXIT

echo "--- starting S3 (Versity), Redis, worker, beat ---"
if ! docker compose up -d --wait s3 redis worker beat; then
    echo "FAIL: docker compose up" >&2
    exit 1
fi

FAILED=0

echo "--- round-tripping a real object through a real S3 server (not moto) ---"
if ! STORAGE_ENDPOINT_URL=http://localhost:9000 \
    STORAGE_BUCKET=smoke-test \
    STORAGE_ACCESS_KEY="$LOCAL_S3_ACCESS_KEY" \
    STORAGE_SECRET_KEY="$LOCAL_S3_SECRET_KEY" \
    uv run python smoke_check_storage.py; then
    echo "FAIL: storage smoke check" >&2
    FAILED=1
fi

echo "--- dispatching a real Celery chord through Redis + the worker/beat containers ---"
if ! docker compose run --rm --no-deps \
    --entrypoint "uv run --no-sync python docker/smoke_check_celery.py" \
    agent-parity; then
    echo "FAIL: celery smoke check" >&2
    FAILED=1
fi

if [[ "$FAILED" -eq 0 ]]; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== SMOKE TEST FAILED ===" >&2
    exit 1
fi
