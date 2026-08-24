"""The app: what is built once, what wraps every request, and what runs it.

`create_app(settings)` exists so the tests can build an app against a temporary
database and a fake embedder without touching the process-wide settings;
`app = create_app()` at the bottom is what `uvicorn contract_analyzer.api.main:app`
imports, which is what `docker/entrypoint.sh` runs.

**The lifespan builds the expensive things once.** The embedder holds an HTTP
client and, for the local provider, a model. `get_http_client` initialises a
process-wide singleton lazily and without a lock, so it is warmed here rather
than raced for by the first two concurrent requests -- and closed on the way
out. The job runner owns the thread pool, and shutting it down is what stops a
reload from leaving analyses running against a closed database.

The **metrics store** is built there too, and is the one thing on `app.state`
that is allowed not to exist: `/metrics/*` answers `503` when it could not be
built, exactly as it did before there was a store at all.

It also **reconciles the analysis record** before the first request: a process
that was killed mid-run left rows saying `running`, and nothing else will ever
close them. They become `interrupted` -- not `failed`, because nothing refused
-- so a client polling one is told to run it again instead of waiting for a
worker that no longer exists.

**Neither key is required to start.** An API that refuses to boot without an
answer key is an API that cannot serve health, the criteria or an upload,
which is most of what a keyless demo wants. The client and the embedder are
built if they can be, and the routes that need one say so with a 503 that names
the `.env` key.

**Every route lives behind `/api`, and the front end is everything else.**
The browser is a client of this API and is served *by* it: `ui/` builds a
static bundle into `api/static/`, which is mounted at `/` after every route so
that a path no route claimed returns `index.html`. That is what makes a hard
refresh on a client-side route work, and it is why there is no CORS
configuration anywhere in this project -- there is only ever one origin.
`/docs`, `/openapi.json` and `/redoc` stay where FastAPI puts them, and
`/health` keeps a hidden alias at the root for the container healthcheck.

**Every request runs inside a trace.** The middleware honours an incoming
`X-Trace-Id` and returns it either way, so one MCP tool call, its HTTP request,
the five criterion runs it starts and every tool call they make share one id in
`.run/app.jsonl`. That is the demo's "here is the same id in the log" moment,
and it costs one context manager.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from ..analyses import reconcile
from ..config import Settings, get_settings
from ..db import get_db
from ..embeddings.base import EmbedderUnavailable, get_embedder
from ..generation.client import AnswerUnavailable, get_client
from ..http_client import get_http_client
from ..logger import configure_logging, get_logger, trace_context
from ..metrics import MetricsStore
from . import errors
from .errors import ApiError
from .jobs import JobRunner
from .routes import analyses, chat, documents, health, metrics
from .schemas import Error

log = get_logger(__name__)

#: Attached to every operation, so the OpenAPI document -- which *is* the
#: connector deliverable -- describes the failure shape as well as the success
#: one, and `Error` appears in `components.schemas` for a generator to bind to.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": Error, "description": description}
    for code, description in {
        400: "Malformed request.",
        401: "Missing or wrong X-API-Key.",
        404: "No such document or analysis.",
        409: "The resource is busy, or already in the requested state.",
        413: "The upload is over api_max_upload_mb.",
        415: "Only PDF uploads are supported.",
        422: "The request body did not validate.",
        500: "Unhandled server error; the X-Trace-Id is on every log line.",
        502: "An upstream call failed after its retries.",
        503: "A key or a dependency the operation needs is not configured.",
    }.items()
}

TITLE = "Contract Analyzer"
DESCRIPTION = """
Compliance analysis of PDF contracts, and cited chat over them.

Upload a contract to get a `document_id`, then bind everything else to it:
analysis and chat both require one, and retrieval is scoped to it in the
library rather than in this API, so an answer cannot cite another contract.

Analyses are jobs. `POST /api/analyses` returns an `analysis_id` in under a
second and the run takes a minute or more; poll `GET /api/analyses/{analysis_id}`
or subscribe to its `/events`. Chat streams by default and can return one JSON
body instead.

Every route is under `/api`. Everything else this server answers is the front
end it also serves, from one origin, which is why no CORS configuration is
needed to call it from a browser.

Errors are always `{"error": {"code", "message", "hint"}}`. `code` is stable
and `hint` says what to do next, which is what a model driving this API through
an MCP server needs in order to recover.
"""


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Any | None = None,
    client: Any | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the application. One call per process, or one per test.

    `embedder`, `client` and `static_dir` are seams, not configuration: passing
    them in is how the tests drive a fake embedder, a scripted model and a
    front-end bundle that is or is not there, without monkeypatching a module
    or depending on whether someone has run `make ui-build` in this checkout.
    Left as None they are built from settings and from `STATIC_DIR`, which is
    what every real process does.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_file)
        # Made now, not at the first upload: a permissions problem in the mount
        # should stop the container starting, not fail one request in an hour.
        settings.raw_dir.mkdir(parents=True, exist_ok=True)
        get_http_client(settings)  # warmed once, rather than raced for
        app.state.settings = settings
        app.state.embedder = embedder if embedder is not None else _embedder(settings)
        app.state.client = client if client is not None else _client(settings)
        app.state.runner = JobRunner(settings, app.state.embedder, app.state.client)
        app.state.metrics = _metrics(settings)
        # Before anything is served: rows a killed process left at `queued` or
        # `running` are not outcomes, and a client polling one would wait for a
        # worker that no longer exists. Its own connection, opened and closed
        # here, because the request pool is not accepting yet.
        interrupted = _reconcile(settings)
        log.info(
            "api.startup",
            extra={
                "db": str(settings.db_path),
                "embedder": settings.embedding_provider,
                "answer_model": settings.answer_model,
                "analysis_model": settings.analysis_model,
                "key_present": bool(settings.anthropic_key),
                "auth_required": bool(settings.api_key_value),
                "api_workers": settings.api_workers,
                "interrupted": interrupted,
            },
        )
        try:
            yield
        finally:
            app.state.runner.shutdown()
            get_http_client(settings).close()
            log.info("api.shutdown")

    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version=health._version(),
        lifespan=lifespan,
        openapi_tags=[
            {"name": "health", "description": "Liveness and the compliance vocabulary."},
            {"name": "documents", "description": "Upload, list, outline, delete."},
            {"name": "analyses", "description": "The five criteria as a background job."},
            {"name": "chat", "description": "Cited question answering over one contract."},
            {"name": "metrics", "description": "KPI data over the metrics store."},
        ],
    )
    # Set before the lifespan runs so that a TestClient built without entering
    # the context manager still has settings to read.
    app.state.settings = settings

    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api_cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Trace-Id"],
        )

    @app.middleware("http")
    async def trace(request: Request, call_next):
        incoming = request.headers.get("X-Trace-Id")
        with trace_context(incoming) as trace_id:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response

    errors.install(app)

    # Every route behind one prefix, so that everything *not* behind it can be
    # the front end. See `_serve_front_end`.
    api = APIRouter(prefix=API_PREFIX)
    for module in (health, documents, analyses, chat, metrics):
        api.include_router(module.router, responses=ERROR_RESPONSES)
    # Last on the router, so it matches only what the real routes did not. An
    # unknown path under /api is a client's mistake and must answer in this
    # API's error envelope; without this it would fall through to the static
    # mount and come back as index.html with a 200, which is the single most
    # confusing failure a generated client can be handed.
    api.add_api_route(
        "/{rest:path}",
        _unknown_route,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    app.include_router(api)

    # The container healthcheck and every `curl localhost:$BACKEND_PORT/health` in the
    # documentation predate the prefix. Hidden from the schema so the OpenAPI
    # document -- which is the connector deliverable -- describes one health
    # operation rather than two spellings of it.
    app.add_api_route("/health", health.health, include_in_schema=False)

    _serve_front_end(app, STATIC_DIR if static_dir is None else static_dir)
    return app


#: One prefix, one reason: the browser and the API share an origin, so
#: everything the API answers has to be distinguishable from everything the
#: front end answers. `/docs`, `/openapi.json` and `/redoc` stay where FastAPI
#: puts them -- they are ahead of the static mount and cannot collide with a
#: client-side route.
API_PREFIX = "/api"

#: Where `ui/vite.config.ts` builds to. A build artefact inside the package: it
#: is what `StaticFiles` serves and it is gitignored.
STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _unknown_route(rest: str) -> None:
    raise ApiError(
        status.HTTP_404_NOT_FOUND,
        "unknown_route",
        f"No API route at {API_PREFIX}/{rest}.",
        "Read /openapi.json for the routes this service publishes.",
    )


class _SinglePageFiles(StaticFiles):
    """`StaticFiles`, plus the fallback a client-side router needs.

    `html=True` is not enough on its own, and it is worth being precise about
    why: it serves `index.html` for a *directory* -- `/` -- and 404s everything
    else with no file behind it. But `/documents/1/analysis` is not a directory
    and never will be a file; it is a route the browser resolves. A hard
    refresh there has to return the app, and without this it returns a 404.

    Only `GET` and `HEAD`, and only a 404. A missing asset under `/assets/` is
    a build that went wrong, and answering it with HTML would turn a broken
    bundle into a blank page with a MIME error in the console instead of a
    clean 404 in the network panel.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or path.startswith("assets/"):
                raise
            return await super().get_response("index.html", scope)


def _serve_front_end(app: FastAPI, directory: Path) -> None:
    """Mount the built front end at `/`, last, if there is one.

    **Last** is the whole trick. Every API route is already registered by the
    time this runs, so the mount only ever sees what they did not claim.

    **If there is one.** The bundle is a build artefact, so a fresh clone, the
    test suite and `make api` before `make ui-build` all run without it. A
    missing directory is a `RuntimeError` out of `StaticFiles`, and an API that
    refuses to start because nobody has run npm is not an improvement on one
    that serves JSON and says where the front end went.
    """
    if not (directory / "index.html").exists():
        log.info("api.static_absent", extra={"path": str(directory)})
        return
    app.mount("/", _SinglePageFiles(directory=directory, html=True), name="ui")
    log.info("api.static_mounted", extra={"path": str(directory)})


def _reconcile(settings: Settings) -> int:
    """Close out analyses a previous process died holding. Never fatal: an API
    that will not start because it could not tidy up is worse than one that
    starts with a stale row, and `GET /health` already reports a database it
    cannot open."""
    try:
        conn = get_db(settings)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        log.warning("api.reconcile_failed", extra={"error": str(exc)})
        return 0
    try:
        return reconcile(conn)
    finally:
        conn.close()


def _metrics(settings: Settings) -> MetricsStore | None:
    """The KPI store, or None if it could not be built.

    None rather than a failed startup, for the same reason as the embedder and
    the answer client: a dashboard is not what makes this service work, and an
    API that refuses to serve an analysis because it could not build a query
    layer has its priorities backwards. `/metrics/*` answers
    `503 metrics_unavailable` in that case, which is what it answered for the
    whole of the previous phase.
    """
    try:
        return MetricsStore(settings)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        log.warning("api.metrics_unavailable", extra={"error": str(exc)})
        return None


def _embedder(settings: Settings):
    """The configured embedder, or None if it cannot be built.

    None rather than a failed startup: `keyword` retrieval needs no embedder, so
    an API without an embedding key still answers questions from an existing
    corpus. Only ingestion actually requires one, and that is where it is
    reported.
    """
    try:
        return get_embedder(settings)
    except (EmbedderUnavailable, ImportError, ValueError) as exc:
        log.warning("api.embedder_unavailable", extra={"error": str(exc)})
        return None


def _client(settings: Settings):
    """The answer client, or None when there is no key. Analysis and chat check
    for None and return `503 no_api_key` rather than queueing work that cannot
    run."""
    try:
        return get_client(settings)
    except AnswerUnavailable as exc:
        log.warning("api.answer_unavailable", extra={"error": str(exc)})
        return None


app = create_app()
