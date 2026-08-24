# syntax=docker/dockerfile:1.7
#
# One image, three stages:
#
#   builder  resolves and installs the dependency set into /opt/venv
#   runtime  that venv + the source tree, running as a non-root user   <- default
#   dev      runtime plus pytest and ruff, for `make docker-test`
#
# The install is deliberately editable (`pip install -e .`). config.py derives
# PROJECT_ROOT from its own location (src/contract_analyzer/config.py -> /app),
# and every relative path in Settings -- db_path, raw_dir, assets_dir, log_file
# -- is anchored to it. A non-editable install would put config.py inside
# site-packages and scatter the database into the venv.

ARG PYTHON_VERSION=3.13

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
RUN mkdir -p src/contract_analyzer && touch src/contract_analyzer/__init__.py

# The HTTP surface and the Streamlit UI are both in the runtime image -- one
# image serves both compose services, and the entrypoint's verb picks which.
# The ~800 MB `local` embedder is not. Override to add it:
#   --build-arg EXTRAS="[api,ui,local]"
ARG EXTRAS="[api,ui]"
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
COPY --chown=app:app . /app
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/entrypoint

RUN chmod +x /usr/local/bin/entrypoint \
    && mkdir -p /app/data/raw /app/data/assets /app/.run "$TIKTOKEN_CACHE_DIR" \
    && chown -R app:app /app/data /app/.run "$TIKTOKEN_CACHE_DIR"

USER app

# 8000 FastAPI, 8501 Streamlit. Both live; the entrypoint verb chooses.
EXPOSE 8000 8501

ENTRYPOINT ["entrypoint"]
CMD ["api"]

# --- dev ---------------------------------------------------------------------
FROM runtime AS dev

USER root
RUN --mount=type=cache,target=/root/.cache/pip pip install -e ".[dev]"
USER app

CMD ["test"]
