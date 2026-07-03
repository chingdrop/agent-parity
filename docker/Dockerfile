# Build context is the repo root (set by docker-compose.yml).
FROM python:3.13-slim

# uv resolves and installs the locked dependency set.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependency layer first so source edits don't bust the cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV DJANGO_SETTINGS_MODULE=config.settings.production
WORKDIR /app/agent_parity_web

# Static assets are baked into the image; SECRET_KEY is only a placeholder to
# satisfy the production settings check at build time.
RUN SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
