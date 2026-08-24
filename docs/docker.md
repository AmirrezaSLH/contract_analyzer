# Docker

`Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`, `.dockerignore` --
a build-and-run foundation for the whole project, laid before the surfaces it
serves exist. Today it builds an image, installs the package and runs the test
suite; the `api` and `ui` verbs are wired and will start working when Phase C
lands, and until then they exit with a message naming the missing module rather
than an `ImportError` from inside uvicorn.

## Stages

| Stage | Contents | Used by |
|---|---|---|
| `base` | `python:3.13-slim-bookworm`, env, `/opt/venv` on PATH | both |
| `builder` | the venv, dependencies resolved against a stub package | build only |
| `runtime` | venv + source, non-root `app` user, `ENTRYPOINT entrypoint`, `CMD api` | `api`, `ui` |
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
  already points at.

`sqlite-vec` needs an interpreter built with loadable-extension support; the
official `python` images have it, which is why the base is that image and not
Alpine (which would also mean source builds for PyMuPDF).

## Entrypoint verbs

`docker/entrypoint.sh` creates `data/raw`, `data/assets` and `.run` (bind mounts
arrive empty) and then dispatches:

| Verb | Runs | State |
|---|---|---|
| `api` | `uvicorn contract_analyzer.api.main:app` on 8000 | Phase C |
| `ui` | `streamlit run src/contract_analyzer/ui/app.py` on 8501 | Phase C |
| `mcp` | `python -m contract_analyzer.mcp.server` (stdio) | Phase C |
| `test` | `pytest -q` | works |
| `lint` | `ruff check src tests scripts` | works |
| `shell` | `bash` | works |
| anything else | exec'd verbatim (`… tools python scripts/ingest.py …`) | works |

## Compose

| Service | Image stage | Ports | Started by `up` |
|---|---|---|---|
| `api` | runtime | 8000 | yes |
| `ui` | runtime | 8501 | yes |
| `tools` | dev | – | no (profile `tools`) |

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
  so with the editable install an edit is live without a rebuild.

## Commands

```bash
make docker-build          # runtime + dev images
make docker-test           # the offline suite, inside the image
make docker-shell          # bash in the dev image, source mounted
make docker-up             # api + ui in the background
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
The healthcheck on `api` polls `/health`, an endpoint that does not exist yet.

## Change log of this document

* 2026-08-23 -- first version: stages, entrypoint verbs, compose layout.
