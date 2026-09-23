# Bare-bones image for running the agent-parity CLI standalone (an analyst's
# laptop, a CI job, an ad-hoc box) without a local uv install. The same
# image also runs the Celery `worker` and `beat` services in
# docker/docker-compose.yml, the scheduled deployment path.
#
# Build from the repo root:
#
#   docker build -f docker/Dockerfile -t agent-parity .
#   docker run --rm -v "$PWD/output:/app/output" agent-parity run --csv
#
# Or via docker/docker-compose.yml: `docker compose -f docker/docker-compose.yml run agent-parity run --csv`

FROM python:3.12-slim

# Official uv static binary — avoids a pip-bootstrap step.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependencies first for layer-cache friendliness: pyproject.toml/uv.lock
# change far less often than agent_parity/ itself.
COPY pyproject.toml uv.lock ./
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
