# Docker

`Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`, `.dockerignore` --
a build-and-run foundation for the whole project. One image: a Node stage
builds the React bundle, the Python runtime serves it at `/` next to the API
on `BACKEND_PORT` (8100). The `mcp` verb runs the MCP connector from the same
image, as a second service that reaches the API over the compose network.

## Stages

| Stage | Contents | Used by |
|---|---|---|
| `ui-builder` | `node:22-slim`, `npm ci` and `vite build` | build only |
| `base` | `python:3.13-slim-bookworm`, env, `/opt/venv` on PATH | both |
| `builder` | the venv, dependencies resolved against a stub package | build only |
| `runtime` | venv + source + the bundle, non-root `app` user, `CMD api` | `api` |
| `dev` | runtime + `.[dev]` (pytest, ruff) | `tools` |

Two details are load-bearing:

* **The install is editable.** `config.py` derives `PROJECT_ROOT` from its own
  location, and every relative path in `Settings` (`db_path`, `raw_dir`,
  `assets_dir`, `log_file`) is anchored to it. A non-editable install would put
  `config.py` under `site-packages` and scatter the database into the venv.
  Editable keeps the source at `/app/src`, so `PROJECT_ROOT` is `/app` exactly
  as it is on the host.
* **Dependencies install against a stub package**, so the layer is keyed on
  `pyproject.toml` alone and editing a module does not re-resolve the tree. The
  real source is copied afterwards, over the same paths the editable finder
  already points at. The bundle is copied from `ui-builder` after that, so a
  host without Node still ships a UI.

`sqlite-vec` needs an interpreter built with loadable-extension support; the
official `python` images have it, which is why the base is that image and not
Alpine (which would also mean source builds for PyMuPDF).

## Entrypoint verbs

`docker/entrypoint.sh` creates `data/raw`, `data/assets` and `.run` (bind mounts
arrive empty) and then dispatches:

| Verb | Runs | State |
|---|---|---|
| `api` | `uvicorn contract_analyzer.api.main:app` on `BACKEND_PORT` (8100); UI at `/` | works |
| `mcp` | `python -m mcp_connector`; `MCP_TRANSPORT` picks stdio or HTTP on `MCP_PORT` (8102) | works |
| `test` | `pytest -q` | works |
| `lint` | `ruff check src tests scripts` | works |
| `shell` | `bash` | works |
| anything else | exec'd verbatim (`… tools python scripts/ingest.py …`) | works |

## Compose

| Service | Image stage | Ports | Started by `up` |
|---|---|---|---|
| `api` | runtime | `BACKEND_PORT` (8100) | yes |
| `mcp` | runtime | `MCP_PORT` (8102) | yes, after `api` is healthy |
| `tools` | dev | – | no (profile `tools`) |

* **The `mcp` service mounts nothing.** `x-app` gives every service the `data/`
  and `.run/` bind mounts; the connector overrides `volumes` to `[]`, because it
  reaches the API over `CA_API_URL` and has no database to open. An empty mount
  list makes that structural rather than a promise in a comment. It also runs
  the HTTP transport rather than stdio: a container's stdin is not where a
  desktop client is. See
  [MCP-Connector/README.md](../MCP-Connector/README.md).
* **One origin.** The API serves the bundle. There is no second UI container
  and no `FRONTEND_PORT` mapping. `api_cors_origins` stays empty because the
  browser never leaves that origin. `FRONTEND_PORT` is still in `.env` for
  `./start.bash --dev` on the host.
* **`api` sets `restart: unless-stopped`** on itself rather than in the shared
  anchor, because `tools` is a one-off container and restarting a finished
  pytest run is not a policy anyone wants.
* **Secrets** come from `.env` at run time (`env_file`, `required: false`) and
  are never baked into a layer; `.dockerignore` excludes `.env` from the build
  context entirely.
* **State** is bind-mounted: `./data` (database, raw PDFs, extracted figures)
  and `./.run` (`app.jsonl`). The three path settings are overridden to
  absolute `/app/...` values so they cannot drift with the working directory.
* **Ownership**: the image is built with `APP_UID`/`APP_GID` build args
  defaulting to 1000, and the Makefile passes the host's. (Not `UID`: bash
  makes that one readonly, so `UID=$(id -u) docker compose build` aborts.) On a
  host user that is not 1000, rebuild rather than `chown` afterwards.
* `tools` additionally mounts `./src`, `./tests` and `./scripts` from the host,
  so with the editable install an edit is live without a rebuild. That overlay
  does not include the bundle; a tools container that needs `/` rebuilt still
  needs an image rebuild.

## Commands

```bash
make docker-build          # runtime + dev images
make docker-test           # the offline suite, inside the image
make docker-shell          # bash in the dev image, source mounted
make docker-up             # api (and the UI it serves) in the background
make docker-logs
make docker-down           # V=1 to drop volumes too
```

Anything else is one-off through `tools`:

```bash
docker compose run --rm tools python scripts/ingest.py data/raw/contract.pdf
docker compose run --rm tools lint
```

## Not done here

Deployment proper: no image registry, no tagged release build, no reverse proxy
or TLS, no non-bind-mount volume strategy, and no CI job building the image.

## Change log of this document

* 2026-08-24 -- Streamlit UI service removed; one process serves API and the
  React bundle. Node `ui-builder` stage. Ports: `BACKEND_PORT` (8100) in
  compose; `FRONTEND_PORT` is host Vite only.
* 2026-08-23 -- first version: stages, entrypoint verbs, compose layout.
