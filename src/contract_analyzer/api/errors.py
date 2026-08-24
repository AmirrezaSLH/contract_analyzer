"""One error shape, one place that maps exceptions onto it.

Every failure leaves this API as `{"error": {"code", "message", "hint"}}`.
`code` is a stable string a client can branch on -- an MCP server's tool result
is read by a model, and "document_not_found" is something it can act on where a
404 alone is not. `hint` is the sentence that says what to do next ("call GET
/documents to list document_id and filename"), which is the difference between
a model retrying blindly and a model retrying correctly.

The handlers here cover the paths that *raise*. One path does not: `ingest_file`
returns its failure rather than raising (`ingest/pipeline.py` is explicit about
it), so `POST /documents` builds the same envelope from `IngestResult.status`
via `from_ingest_error`. Both routes must answer with the same `code` for the
same cause -- a missing embedding key is `embedder_unavailable` whether it
surfaced as an exception or as a status -- and `tests/test_api.py` asserts it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..embeddings.base import EmbedderUnavailable
from ..embeddings.guard import ModelMismatch
from ..generation.client import AnswerUnavailable
from ..http_client import HttpFailure
from ..logger import current_trace_id, get_logger
from .schemas import Error, ErrorBody

log = get_logger(__name__)


class ApiError(Exception):
    """A failure with its HTTP status and its stable code already decided.

    Raised by routes for the cases that are *not* an exception from the library
    -- an unknown id, a file that is not a PDF, a job that is still running.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        hint: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint
        self.headers = headers or {}


# -- the shortcuts the routes actually use ---------------------------------


def document_not_found(document_id: Any) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "document_not_found",
        f"No document with id {document_id}.",
        "Call GET /documents to list document_id and filename, or POST /documents to upload one.",
    )


def analysis_not_found(analysis_id: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "analysis_not_found",
        f"No analysis with id {analysis_id}.",
        "Call GET /analyses?document_id=... to list the analyses of one document.",
    )


def no_api_key() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "no_api_key",
        "ANTHROPIC_API_KEY is not set, so the answer model cannot be called.",
        "Set ANTHROPIC_API_KEY in .env and restart. Upload and retrieval work without it.",
    )


def unauthorized() -> ApiError:
    return ApiError(
        status.HTTP_401_UNAUTHORIZED,
        "unauthorized",
        "This API requires an X-API-Key header.",
        "Send the key configured as API_KEY. /health and /criteria are open.",
    )


def from_ingest_error(error: str | None) -> ApiError:
    """`IngestResult.error` -- `"EmbedderUnavailable: ..."` -- as an envelope.

    `ingest_file` catches everything and reports it, so this is the only way an
    ingest failure ever becomes a status code. The leading exception name is
    what carries the meaning; the rest is the message the library wrote, which
    already names the `.env` key when that is the problem.
    """
    name, _, message = (error or "").partition(": ")
    message = message or error or "Ingestion failed."
    if name == EmbedderUnavailable.__name__:
        return ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "embedder_unavailable", message,
            "Set the embedding provider's key in .env, or set embedding_provider to "
            "'fake' in settings.json to run offline.",
        )
    if name in ("FileNotFoundError", "ValueError"):
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation", message)
    return ApiError(status.HTTP_502_BAD_GATEWAY, "ingest_failed", message)


# -- the handlers ----------------------------------------------------------

#: Library exceptions that already mean something specific over HTTP.
_MAPPED: tuple[tuple[type[Exception], int, str, str | None], ...] = (
    (
        AnswerUnavailable, status.HTTP_503_SERVICE_UNAVAILABLE, "answer_unavailable",
        "Check ANTHROPIC_API_KEY in .env.",
    ),
    (
        EmbedderUnavailable, status.HTTP_503_SERVICE_UNAVAILABLE, "embedder_unavailable",
        "Set the embedding provider's key in .env, or use embedding_provider 'fake'.",
    ),
    (
        ModelMismatch, status.HTTP_409_CONFLICT, "model_mismatch",
        "The database was built with a different embedding model. Re-ingest, or point "
        "DB_PATH at the database that matches.",
    ),
    (
        HttpFailure, status.HTTP_502_BAD_GATEWAY, "upstream_failure",
        "The upstream API did not answer after the transport's retries. Try again.",
    ),
)


def install(app: FastAPI) -> None:
    """Register the handlers. Called once, by `create_app`."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _response(exc.status_code, exc.code, exc.message, exc.hint, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation", _first(exc.errors()),
            "See the OpenAPI schema at /openapi.json for the expected body.",
        )

    @app.exception_handler(ValidationError)
    async def _model_invalid(request: Request, exc: ValidationError) -> JSONResponse:
        return _response(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation", _first(exc.errors())
        )

    for exc_type, code_status, code, hint in _MAPPED:
        app.add_exception_handler(exc_type, _mapper(code_status, code, hint))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The message is not echoed: an unhandled exception's text is as likely
        # to be a file path as an explanation. The trace id is, and it is what
        # turns a support question into a grep of app.jsonl.
        log.exception("api.unhandled", extra={"path": request.url.path})
        return _response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "internal",
            "The server failed to handle this request.",
            "The response's X-Trace-Id appears on every log line of this request.",
        )


def _mapper(status_code: int, code: str, hint: str | None):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        log.info("api.error", extra={"code": code, "path": request.url.path})
        return _response(status_code, code, str(exc), hint)

    return handler


def _response(
    status_code: int,
    code: str,
    message: str,
    hint: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Error(error=ErrorBody(code=code, message=message, hint=hint))
    trace_id = current_trace_id()
    merged = dict(headers or {})
    if trace_id:
        merged.setdefault("X-Trace-Id", trace_id)
    return JSONResponse(
        status_code=status_code, content=body.model_dump(exclude_none=True), headers=merged
    )


def _first(errors: list[Any]) -> str:
    """One readable sentence out of pydantic's list, rather than the list."""
    if not errors:
        return "Invalid request."
    first = errors[0]
    location = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    message = first.get("msg", "invalid")
    return f"{location}: {message}" if location else message


__all__ = [
    "ApiError",
    "analysis_not_found",
    "document_not_found",
    "from_ingest_error",
    "install",
    "no_api_key",
    "unauthorized",
]
