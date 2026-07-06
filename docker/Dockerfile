# Bare-bones image for running the agent-parity CLI standalone (an analyst's
# laptop, a CI job, an ad-hoc box) without a local uv install. cyberhub
# consumes this package as a pinned git dependency and has its own
# deployment/container story that supersedes this entirely — this Dockerfile
# only exists to run the CLI on its own when there's no reason to pull in
# the whole hub.
#
# Build from the repo root (the build context must include
# vendor/py-shared-tools — run `git submodule update --init` first if you
# haven't already):
#
#   docker build -f docker/Dockerfile -t agent-parity .
#   docker run --rm -v "$PWD/output:/app/output" agent-parity run
#
# Or via docker/docker-compose.yml: `docker compose -f docker/docker-compose.yml run agent-parity run`

FROM python:3.12-slim

# Official uv static binary — avoids a pip-bootstrap step.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependencies first for layer-cache friendliness: pyproject.toml/uv.lock and
# the vendored submodule change far less often than agent_parity/ itself.
COPY pyproject.toml uv.lock ./
COPY vendor/ vendor/
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 parity && chown -R parity:parity /app
USER parity

# --no-sync: without it, "uv run" re-resolves against uv.lock's full [dev]
# group on every container start, silently reinstalling moto/boto3-stubs/etc.
# that --no-dev deliberately excluded from the image — slower, and would fail
# outright with no network access.
ENTRYPOINT ["uv", "run", "--no-sync", "agent-parity"]
CMD ["--help"]
