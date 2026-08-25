# syntax=docker/dockerfile:1.7
#
# One image, four stages:
#
#   ui-builder  Node: Vite build of ui/ into the API static directory
#   builder     resolves and installs the Python dependency set into /opt/venv
#   runtime     that venv + the source tree + the bundle, non-root   <- default
#   dev         runtime plus pytest and ruff, for `make docker-test`
#
# The install is deliberately editable (`pip install -e .`). config.py derives
# PROJECT_ROOT from its own location (src/contract_analyzer/config.py -> /app),
# and every relative path in Settings -- db_path, raw_dir, assets_dir, log_file
# -- is anchored to it. A non-editable install would put config.py inside
# site-packages and scatter the database into the venv.
#
# Copy the tree by path, not `COPY .`: presentation/, design/ and the plan
# docs are not runtime, and a catch-all would bake them into every layer.

ARG PYTHON_VERSION=3.13
ARG NODE_VERSION=22

# --- front end ---------------------------------------------------------------
# Isolated from the Python image: production needs the static files, not Node.
FROM node:${NODE_VERSION}-slim AS ui-builder

WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY ui/ ./
# vite.config.ts writes to ../src/contract_analyzer/api/static
RUN npm run build

# --- base --------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # tokens.py falls back to characters/4 without it, but a warm cache means
    # the container never reaches for the BPE table at request time.
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app

# --- builder -----------------------------------------------------------------
FROM base AS builder

RUN python -m venv "$VIRTUAL_ENV"

# Dependencies first, against a stub package: this layer is keyed on
# pyproject.toml alone, so editing source does not re-resolve the tree.
COPY pyproject.toml ./
# Both package roots, because `packages.find` is told to look in both. A stub
# for each is enough: the real source arrives in the runtime stage, over the
# same paths the editable install already points at.
RUN mkdir -p src/contract_analyzer MCP-Connector/mcp_connector \
    && touch src/contract_analyzer/__init__.py MCP-Connector/mcp_connector/__init__.py

# The HTTP surface (which also serves the built UI) and the MCP connector are
# both in the runtime image: one image, one `entrypoint` verb per surface.
# The ~800 MB `local` embedder is not. Override to add it:
#   --build-arg EXTRAS="[api,mcp,local]"
ARG EXTRAS="[api,mcp]"
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -e ".${EXTRAS}"

# --- runtime -----------------------------------------------------------------
FROM base AS runtime

# Matched to the host user by default so bind-mounted data/ and .run/ stay
# writable and are not left owned by root. Named APP_UID rather than UID: UID is
# readonly in bash, so `UID=$(id -u) docker compose build` would abort there.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /bin/bash app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app pyproject.toml settings.json ./
COPY --chown=app:app src ./src
COPY --chown=app:app MCP-Connector ./MCP-Connector
COPY --chown=app:app scripts ./scripts
COPY --from=ui-builder --chown=app:app /app/src/contract_analyzer/api/static \
     /app/src/contract_analyzer/api/static
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/entrypoint

RUN chmod +x /usr/local/bin/entrypoint \
    && mkdir -p /app/data/raw /app/data/assets /app/.run "$TIKTOKEN_CACHE_DIR" \
    && chown -R app:app /app/data /app/.run "$TIKTOKEN_CACHE_DIR"

USER app

# FastAPI, and the built UI at `/`. 8102 is the MCP connector, and only when
# it is run with MCP_TRANSPORT=http -- on stdio it binds nothing.
EXPOSE 8100 8102

ENTRYPOINT ["entrypoint"]
CMD ["api"]

# --- dev ---------------------------------------------------------------------
FROM runtime AS dev

USER root
COPY --chown=app:app tests ./tests
RUN --mount=type=cache,target=/root/.cache/pip pip install -e ".[dev]"
USER app

CMD ["test"]
