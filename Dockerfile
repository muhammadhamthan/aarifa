# syntax=docker/dockerfile:1
# =============================================================================================
# Kairox platform image: one image, three modes (`kx api` | `kx worker` | `kx voice`) plus the
# one-off `kx migrate` and the db-bootstrap script. Built for linux/arm64 (Graviton Fargate).
#
#   docker buildx build --platform linux/arm64 -t kairox/app:dev .
#   docker run --rm kairox/app:dev api
# =============================================================================================
ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7

# checkov:skip=CKV_DOCKER_7: base images are pinned through the ARGs above
FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /bin/

ARG APP_PACKAGE=arifaa-pack
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# 1. Third-party dependencies only (cached until uv.lock or a pyproject changes).
COPY pyproject.toml uv.lock ./
COPY packages/kx-core-db/pyproject.toml packages/kx-core-db/
COPY packages/kx-core-crm/pyproject.toml packages/kx-core-crm/
COPY packages/kx-core-automation/pyproject.toml packages/kx-core-automation/
COPY packages/kx-agent-framework/pyproject.toml packages/kx-agent-framework/
COPY apps/arifaa-pack/pyproject.toml apps/arifaa-pack/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${APP_PACKAGE}"

# 2. Workspace packages, installed as wheels (non-editable) so /src is not needed at runtime.
COPY packages packages
COPY apps apps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${APP_PACKAGE}"

# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# WeasyPrint (quote / requirement-summary PDFs): Pango + HarfBuzz + fontconfig, and fonts for
# Latin, Arabic, Devanagari, Tamil, Sinhala (fonts-noto-core) and Chinese/Japanese/Korean
# (fonts-noto-cjk). postgresql-client runs the db-bootstrap script.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libfontconfig1 \
      fontconfig \
      shared-mime-info \
      fonts-noto-core \
      fonts-noto-cjk \
      fonts-dejavu-core \
      postgresql-client \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 kx \
 && useradd --system --uid 10001 --gid kx --home-dir /app --no-create-home --shell /usr/sbin/nologin kx

WORKDIR /app

COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root db/ddl /app/db/ddl
COPY --chown=root:root --chmod=0755 infra/scripts/db_bootstrap.sh /app/scripts/db_bootstrap.sh
COPY --chown=root:root docker/healthcheck.py /app/healthcheck.py

# The root filesystem is read-only on ECS; /tmp is a writable task volume.
ENV PATH="/app/.venv/bin:${PATH}" \
    KX_DDL_DIR=/app/db/ddl \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/tmp \
    KX_API_PORT=8000

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["kx"]
CMD ["api"]
