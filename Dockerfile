FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
# Libraries only, never the code: these layers are rebuilt when uv.lock changes, not
# when the app does. The app runs from /srv/app/app (`python -m app...`), so it is not
# installed into the environment.
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /uvx /bin/
# A slow connection needs longer than the default 30 seconds per download.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_HTTP_TIMEOUT=300
WORKDIR /srv/app
COPY pyproject.toml uv.lock ./
# The cache mount keeps what was downloaded, so an interrupted build resumes.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

# The worker also parses uploaded documents, so only its image carries those libraries.
FROM builder AS worker-builder
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --extra knowledge --no-install-project

FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/srv/app/.venv/bin:$PATH"
RUN groupadd --gid 10001 frontdesk && useradd --uid 10001 --gid frontdesk --no-create-home frontdesk
WORKDIR /srv/app
RUN mkdir -p /srv/app/data/knowledge /srv/app/data/models && chown -R frontdesk:frontdesk /srv/app/data

FROM base AS runtime
COPY --from=builder --chown=frontdesk:frontdesk /srv/app/.venv ./.venv
# The code last: a code change rebuilds only these layers.
COPY --chown=frontdesk:frontdesk alembic.ini ./alembic.ini
COPY --chown=frontdesk:frontdesk migrations ./migrations
COPY --chown=frontdesk:frontdesk app ./app
USER frontdesk
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"
# Uvicorn with the same options, but calls in progress may finish when it is stopped.
CMD ["python", "-m", "app.serve"]

FROM base AS worker
RUN apt-get update && apt-get install --no-install-recommends -y libgl1 libglib2.0-0     && rm -rf /var/lib/apt/lists/*
COPY --from=worker-builder --chown=frontdesk:frontdesk /srv/app/.venv ./.venv
COPY --chown=frontdesk:frontdesk alembic.ini ./alembic.ini
COPY --chown=frontdesk:frontdesk migrations ./migrations
COPY --chown=frontdesk:frontdesk app ./app
USER frontdesk
HEALTHCHECK NONE
CMD ["python", "-m", "app.worker"]
