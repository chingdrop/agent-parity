#!/usr/bin/env bash
# Docker Compose integration smoke test.
#
# Proves the things the fast, offline `uv run pytest` suite structurally
# can't: the Dockerfile actually builds, migrations run against a real
# Postgres, a real Celery worker (not task_always_eager) picks up and
# completes a group+chord dispatched through a real Redis broker/backend,
# and agent_parity/storage.py round-trips a real object through a real
# MinIO server (not moto). Needs Docker; not part of `uv run pytest` or any
# fast/CI path — run manually, e.g. before a release.
#
# Usage: docker/smoke_test.sh [--keep]
#   --keep   leave the stack running on exit (default: always tears down)

set -uo pipefail
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

FAILED=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; FAILED=1; }

cleanup() {
    if [[ "$KEEP" -eq 1 ]]; then
        echo "--- --keep passed: leaving the stack running ---"
        return
    fi
    echo "--- tearing down ---"
    docker compose down -v --remove-orphans
}
trap cleanup EXIT

echo "--- building images ---"
if ! docker compose build; then
    fail "docker compose build"
    exit 1
fi

echo "--- starting stack ---"
if ! docker compose up -d; then
    fail "docker compose up"
    exit 1
fi

echo "--- waiting for web to become reachable ---"
web_up=0
for _ in $(seq 1 30); do
    if curl -fsS http://localhost:8000/ >/dev/null 2>&1; then
        web_up=1
        break
    fi
    sleep 2
done
if [[ "$web_up" -eq 1 ]]; then
    pass "web is reachable"
else
    fail "web did not become reachable within 60s"
    docker compose logs web
    exit 1
fi

echo "--- seeding demo data (proves migrations + ORM against a real Postgres) ---"
if docker compose exec -T web python manage.py seed_demo; then
    pass "seed_demo ran inside the web container"
else
    fail "seed_demo failed"
fi

echo "--- checking the dashboard reflects the seeded data ---"
overview="$(curl -fsS http://localhost:8000/ || true)"
if echo "$overview" | grep -q "Acme Corp"; then
    pass "overview page shows seeded client data"
else
    fail "overview page did not show expected data"
fi

echo "--- dispatching a real Celery chord (real Redis broker/backend, real worker) ---"
if docker compose exec -T web python manage.py smoke_check_celery; then
    pass "Celery group/chord completed via the real worker/broker"
else
    fail "Celery smoke check failed"
fi

echo "--- round-tripping a real object through MinIO (not moto) ---"
if docker compose exec -T \
    -e STORAGE_ENDPOINT_URL=http://minio:9000 \
    -e STORAGE_BUCKET=smoke-test \
    -e STORAGE_ACCESS_KEY="$MINIO_ROOT_USER" \
    -e STORAGE_SECRET_KEY="$MINIO_ROOT_PASSWORD" \
    web python manage.py smoke_check_storage; then
    pass "object storage round trip works against the real MinIO server"
else
    fail "object storage smoke check failed"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== SOME CHECKS FAILED (see above) ==="
    exit 1
fi
