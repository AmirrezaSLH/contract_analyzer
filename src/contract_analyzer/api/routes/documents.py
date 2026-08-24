"""Upload a contract, list what is stored, read an outline, delete one.

This is the only route module that writes to the corpus, and the only one that
handles bytes from outside the process. Everything sharp about that is in
`uploads.py`: the filename is sanitized before it becomes a path, the size cap
is enforced while the body streams to disk, and the assembled path is checked to
be inside `RAW_DIR`.

The other thing to know is that `ingest_file` **does not raise**. Its contract
is to report what happened -- a missing embedding key comes back as
`IngestResult(status="failed", error="EmbedderUnavailable: ...")` -- so a
handler that only mapped exceptions would answer `201` with zero chunks. The
status is branched on here, and `errors.from_ingest_error` turns it into the
same envelope, with the same `code`, that the exception path would have
produced.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from ...analyses import LastAnalysis, last_analysis_by_document, live_analyses
from ...documents import (
    delete_document,
    document_sections,
    get_document,
    list_documents,
    set_filename,
)
from ...ingest.pipeline import ingest_file
from ..deps import ConnDep, EmbedderDep, Protected, RunnerDep, SettingsDep
from ..errors import ApiError, document_not_found, from_ingest_error
from ..jobs import summaries_for
from ..schemas import (
    DocumentDetail,
    DocumentOut,
    LastAnalysisOut,
    SectionOut,
    UploadOut,
)
from ..uploads import require_pdf, save_upload, stored_path

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Protected])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a contract and index it",
    description=(
        "Parses, chunks and embeds the PDF, and returns the `document_id` every other "
        "endpoint needs. Each upload mints a new id even for identical bytes, so two "
        "sessions working on the same contract cannot see each other's analyses."
    ),
)
async def upload(
    settings: SettingsDep,
    conn: ConnDep,
    embedder: EmbedderDep,
    # Annotated rather than a default: a call in a default is evaluated once at
    # import, which ruff flags and which would share one File() across routes.
    file: Annotated[UploadFile, File(description="The contract, as a PDF.")],
) -> UploadOut:
    require_pdf(file.filename, file.content_type)
    if embedder is None:
        # Checked before the bytes are written rather than after: without an
        # embedder `ingest_file` fails on an assertion, which would surface as
        # a 502 about SQLite instead of the 503 that names the missing key.
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "embedder_unavailable",
            f"No embedder is available for provider '{settings.embedding_provider}', "
            "so a contract cannot be indexed.",
            "Set the provider's key in .env, or set embedding_provider to 'fake' in "
            "settings.json to run offline.",
        )
    path = stored_path(settings.raw_dir, file.filename)
    await save_upload(file, path, max_bytes=settings.max_upload_bytes)

    # Off the event loop: parsing, chunking and embedding a contract is seconds
    # of blocking work, and this handler has to be `async` for `await
    # file.read()`. Left inline it would stall every other request in flight.
    started = time.perf_counter()
    result = await run_in_threadpool(ingest_file, path, conn, embedder, settings)
    if not result.ok or result.document_id is None:
        path.unlink(missing_ok=True)
        raise from_ingest_error(result.error)

    # The bytes live under `<uuid>-<sanitized name>`; the client is shown the
    # name it sent. See `documents.set_filename` for why that split is in the
    # catalogue rather than here.
    if file.filename:
        set_filename(conn, result.document_id, file.filename)
    document = get_document(conn, result.document_id)
    assert document is not None  # just written, in this transaction
    return UploadOut(
        document_id=document.document_id,
        filename=document.filename,
        pages=document.page_count,
        chunks=document.chunks,
        spine_source=document.spine_source,
        ingested_at=document.ingested_at,
        elapsed_s=round(time.perf_counter() - started, 3),
    )


@router.get("", summary="Every stored contract, newest first")
def index(conn: ConnDep) -> list[DocumentOut]:
    """What a client binds a session to, with enough to draw a row for each.

    `last_analysis` comes from one query over the whole list rather than one
    per document: this is the endpoint a library table renders from.
    """
    documents = list_documents(conn)
    last = last_analysis_by_document(conn, [d.document_id for d in documents])
    return [_out(d, last.get(d.document_id)) for d in documents]


@router.get("/{document_id}", summary="One contract and its analyses")
def detail(document_id: int, conn: ConnDep, runner: RunnerDep) -> DocumentDetail:
    document = get_document(conn, document_id)
    if document is None:
        raise document_not_found(document_id)
    last = last_analysis_by_document(conn, [document_id]).get(document_id)
    return DocumentDetail(
        # The same merge `GET /analyses` uses, from the same function, so the
        # two lists cannot disagree about a document's history.
        **_out(document, last).model_dump(),
        analyses=summaries_for(runner, conn, document_id),
    )


@router.get("/{document_id}/sections", summary="The contract's outline")
def sections(document_id: int, conn: ConnDep) -> list[SectionOut]:
    """Built from the indexed chunks, so every section listed is one retrieval
    can actually reach -- see `documents.py`."""
    if get_document(conn, document_id) is None:
        raise document_not_found(document_id)
    return [
        SectionOut(path=s.path, title=s.title, page_display=s.page_display, chunks=s.chunks)
        for s in document_sections(conn, document_id)
    ]


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a contract, its chunks, its vectors and its file",
    description="The analyses of this contract and their reports are kept: the report is "
                "the deliverable and it is self-contained. Refused with `409` while an "
                "analysis of it is still queued or running, in this process or another.",
)
def remove(document_id: int, conn: ConnDep, settings: SettingsDep) -> Response:
    if get_document(conn, document_id) is None:
        raise document_not_found(document_id)
    # A query rather than a look in this worker's job dict, so a run started by
    # another worker -- or by `make analyze` against the same database -- also
    # blocks the delete.
    running = live_analyses(conn, document_id)
    if running:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "analysis_running",
            f"Analysis {running[0].analysis_id} is still running on this document.",
            "Cancel that analysis, or wait for it to finish, then delete the contract.",
        )
    # The analyses are deliberately left behind: `analyses.document_id` carries
    # no foreign key, because the report is the deliverable and it is
    # self-contained. See `analyses.py`.
    delete_document(conn, document_id, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _out(document, last: LastAnalysis | None = None) -> DocumentOut:
    return DocumentOut(
        document_id=document.document_id,
        filename=document.filename,
        pages=document.page_count,
        chunks=document.chunks,
        spine_source=document.spine_source,
        ingested_at=document.ingested_at,
        last_analysis=None if last is None else LastAnalysisOut(
            analysis_id=last.analysis_id,
            status=last.status,
            completed_at=last.completed_at,
            states=last.states,
            needs_review=last.needs_review,
        ),
    )
