# syntax=docker/dockerfile:1.7

# --- Build stage ------------------------------------------------------------
FROM python:3.13-slim AS build

# uv handles dependency resolution + install from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install deps first (cached when only source changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=s/e3449a2d-58f7-48f3-a848-055059346fd1-uv,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install the project itself.
COPY src/ ./src/
RUN --mount=type=cache,id=s/e3449a2d-58f7-48f3-a848-055059346fd1-uv,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- Runtime stage ----------------------------------------------------------
FROM python:3.13-slim AS runtime

# Drop build-only tooling; copy only the prebuilt venv + source.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/src /app/src

# Run as non-root.
RUN useradd --create-home --uid 1000 worker
USER worker
WORKDIR /app

# SIGTERM triggers the clean-drain handler in doc_worker.worker.
STOPSIGNAL SIGTERM

CMD ["doc-worker"]
