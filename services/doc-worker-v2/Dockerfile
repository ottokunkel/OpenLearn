# syntax=docker/dockerfile:1.7
#
# Build context is the workspace root: services/. The image needs the workspace
# `pyproject.toml` + `uv.lock` plus all referenced workspace members
# (doc-worker-v2, storage-supabase, storage-postgres) so uv can resolve.
#
#   docker build -f services/doc-worker-v2/Dockerfile services/
#
# Cache mount IDs must be prefixed with the Railway service ID
# (`s/<svc-id>-...`) or Railway rejects the build. Current v2 service:
# document-worker-v2 → 3f705c5c-aa88-4f53-ae0e-95af43eb4d7a.

# --- Build stage ------------------------------------------------------------
FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.1 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Workspace root + every member uv needs to resolve.
COPY pyproject.toml uv.lock ./
COPY doc-worker-v2/ ./doc-worker-v2/
COPY storage-supabase/ ./storage-supabase/
COPY storage-postgres/ ./storage-postgres/

WORKDIR /app/doc-worker-v2
RUN --mount=type=cache,id=s/3f705c5c-aa88-4f53-ae0e-95af43eb4d7a-uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- Runtime stage ----------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=build /opt/venv /opt/venv

# Run as non-root.
RUN useradd --create-home --uid 1000 worker
USER worker
WORKDIR /home/worker

# SIGTERM triggers the clean-drain handler in doc_worker_v2.worker.
STOPSIGNAL SIGTERM

CMD ["doc-worker-v2"]
