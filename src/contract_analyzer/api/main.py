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

**Neither key is required to start.** An API that refuses to boot without an
answer key is an API that cannot serve `/health`, `/criteria` or an upload,
which is most of what a keyless demo wants. The client and the embedder are
built if they can be, and the routes that need one say so with a 503 that names
the `.env` key.

**Every request runs inside a trace.** The middleware honours an incoming
`X-Trace-Id` and returns it either way, so one MCP tool call, its HTTP request,
the five criterion runs it starts and every tool call they make share one id in
`.run/app.jsonl`. That is the demo's "here is the same id in the log" moment,
and it costs one context manager.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings, get_settings
from ..embeddings.base import EmbedderUnavailable, get_embedder
from ..generation.client import AnswerUnavailable, get_client
from ..http_client import get_http_client
from ..logger import configure_logging, get_logger, trace_context
from . import errors
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

Analyses are jobs. `POST /analyses` returns an `analysis_id` in under a second
and the run takes a minute or more; poll `GET /analyses/{analysis_id}` or
subscribe to its `/events`. Chat streams by default and can return one JSON
body instead.

Errors are always `{"error": {"code", "message", "hint"}}`. `code` is stable
and `hint` says what to do next, which is what a model driving this API through
an MCP server needs in order to recover.
"""


def create_app(
    settings: Settings | None = None,
    *,
    embedder: Any | None = None,
    client: Any | None = None,
) -> FastAPI:
    """Build the application. One call per process, or one per test.

    `embedder` and `client` are seams, not configuration: passing them in is how
    the tests drive a fake embedder and a scripted model without monkeypatching
    a module, and it is why the job runner can be handed the same scripted
    client the request handlers use. Left as None they are built from settings,
    which is what every real process does.
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
        log.info(
            "api.startup",
            extra={
                "db": str(settings.db_path),
                "embedder": settings.embedding_provider,
                "answer_model": settings.answer_model,
                "key_present": bool(settings.anthropic_key),
                "auth_required": bool(settings.api_key_value),
                "api_workers": settings.api_workers,
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
    for module in (health, documents, analyses, chat, metrics):
        app.include_router(module.router, responses=ERROR_RESPONSES)
    return app


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
