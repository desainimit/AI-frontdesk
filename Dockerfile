FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/
# A slow connection needs longer than the default 30 seconds per download.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_HTTP_TIMEOUT=300
WORKDIR /srv/app
COPY pyproject.toml uv.lock ./
# The cache mount keeps what was downloaded, so an interrupted build resumes.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/srv/app/.venv/bin:$PATH"
RUN groupadd --gid 10001 frontdesk && useradd --uid 10001 --gid frontdesk --no-create-home frontdesk
WORKDIR /srv/app
RUN mkdir -p /srv/app/data/knowledge /srv/app/data/models && chown -R frontdesk:frontdesk /srv/app/data
COPY --from=builder --chown=frontdesk:frontdesk /srv/app/.venv ./.venv
COPY --chown=frontdesk:frontdesk app ./app
COPY --chown=frontdesk:frontdesk migrations ./migrations
COPY --chown=frontdesk:frontdesk alembic.ini ./alembic.ini
USER frontdesk
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "20", "--ws-max-size", "16384", "--ws-max-queue", "16"]

# The worker also parses uploaded documents, so only its image carries those libraries.
FROM builder AS worker-builder
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --extra knowledge --no-editable

FROM runtime AS worker
USER root
RUN apt-get update && apt-get install --no-install-recommends -y libgl1 libglib2.0-0     && rm -rf /var/lib/apt/lists/*
COPY --from=worker-builder --chown=frontdesk:frontdesk /srv/app/.venv ./.venv
USER frontdesk
HEALTHCHECK NONE
CMD ["python", "-m", "app.worker"]
