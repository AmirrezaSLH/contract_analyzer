"""parse -> chunk -> embed -> store, once per file, idempotently.

This is where the four tables finally get rows in them. Three properties are
worth more than the code that provides them:

**Re-ingesting an unchanged file is free.** The file's SHA-256 is compared
against `documents.content_hash` *before* parsing, not after: parsing costs
about a second a contract and embedding costs money, and neither should be
spent to discover that nothing changed. Running `make ingest` twice in a row
is the demonstration.

**A changed file is replaced whole.** The `documents` row is deleted, and the
cascade plus the FTS triggers take `chunks`, `chunks_vec` and `chunks_fts`
with it. Incremental per-page re-ingestion is explicitly out of scope; at a
hundred chunks, rebuilding is a second and has no failure modes.

**One file's failure is one file's problem.** A PDF that raises is recorded as
`failed` and the run continues -- a batch must not be hostage to its worst
file.

The model guard runs before any of it. Vectors from two different embedders are
points in unrelated spaces; a database holding both returns rankings that look
reasonable and mean nothing, and nothing about the symptom points at the cause.
So the run is refused, by name, up front.

Every stage is wrapped in a `span()` from `logger.py`, so `.run/app.jsonl`
carries the same parse / chunk / embed / write timings the ingest report
prints, under one trace id per file. That is the seam the KPI page hangs off:
the metrics store's handler turns each of those `span.end` records into a row
without this module knowing it exists. `ingest.embed` is the one that carries
a dollar figure -- `tokens` and `cost_usd` from the embedder's reported usage
-- and it is captured rather than tiled, because at four orders of magnitude
under the analysis it enables it is a sentence in the waterfall and not a
number on a dashboard.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Literal

from ..config import PROJECT_ROOT, Settings, get_settings
from ..embeddings.base import Embedder

# The guard lives in `embeddings/` because retrieval needs it too and the read
# path must not import the write path. Re-exported here because ingestion is
# where a caller expects to find it.
from ..embeddings.guard import (
    ModelMismatch,
    check_embedding_model,
    check_query_model,
    stored_embedding_models,
)
from ..generation.pricing import embedding_cost_usd
from ..logger import get_logger, span, trace_context
from ..models import Chunk
from ..parse.figures import asset_dir
from ..parse.pdf import ParsedDocument, file_hash, parse_pdf
from .chunker import ChunkingReport, chunk_document

log = get_logger(__name__)

#: What `ingest_paths` picks up when handed a directory. The parser is
#: PDF-only today; a `.docx` loader that emits the same elements would add its
#: suffix here and change nothing else.
KNOWN_SUFFIXES = frozenset({".pdf"})

IngestStatus = Literal["ingested", "replaced", "skipped", "failed", "dry-run"]

#: `text-embedding-3-small`, dollars per token. Used only to print an estimate.
OPENAI_COST_PER_TOKEN = 0.02 / 1_000_000


@dataclass
class IngestResult:
    """What happened to one file. Enough for the CLI's table, and for tests."""

    path: Path
    status: IngestStatus
    document_id: int | None = None
    pages: int = 0
    elements: int = 0
    chunks: int = 0
    #: Tokens actually sent to the embedder -- what the cost estimate is of.
    tokens: int = 0
    median_tokens: int = 0
    max_tokens: int = 0
    elapsed: float = 0.0
    #: Where the section breadcrumbs came from, carried out so the CLI can say
    #: "sections inferred" without re-opening the file.
    spine_source: str = "none"
    report: ChunkingReport | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "failed"


@dataclass
class _Prepared:
    """A file parsed and chunked, but not yet written."""

    parsed: ParsedDocument
    chunks: list[Chunk]
    report: ChunkingReport = field(default_factory=ChunkingReport)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def collect_paths(paths: Iterable[Path | str], settings: Settings) -> list[Path]:
    """Expand directories into the files this pipeline knows how to read."""
    found: list[Path] = []
    for entry in paths or [settings.raw_dir]:
        entry = Path(entry)
        if entry.is_dir():
            found.extend(
                p for p in sorted(entry.rglob("*")) if p.suffix.lower() in KNOWN_SUFFIXES
            )
        else:
            found.append(entry)
    # A path given twice (explicitly and via its directory) must not be
    # ingested twice; `documents.path` is unique, so the second would fail.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def ingest_paths(
    paths: Sequence[Path | str],
    conn: sqlite3.Connection | None,
    embedder: Embedder | None,
    settings: Settings | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    describe_figures: bool = False,
    on_result: Callable[[IngestResult], None] | None = None,
) -> list[IngestResult]:
    """Ingest every path given. Directories are walked; failures do not abort.

    `on_result`, if callable, is invoked with each `IngestResult` as it lands,
    so the CLI can print a file's line while the next one parses.
    """
    settings = settings or get_settings()
    if not dry_run:
        if conn is None or embedder is None:
            raise ValueError("a connection and an embedder are required unless dry_run")
        # Once, before any parsing: a mismatch is a property of the run, and
        # discovering it on file two means a wasted parse and an embedding
        # bill for file one.
        check_embedding_model(conn, embedder)

    results: list[IngestResult] = []
    for path in collect_paths(paths, settings):
        result = ingest_file(
            path,
            conn,
            embedder,
            settings,
            force=force,
            dry_run=dry_run,
            describe_figures=describe_figures,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def ingest_file(
    path: Path | str,
    conn: sqlite3.Connection | None,
    embedder: Embedder | None,
    settings: Settings | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    describe_figures: bool = False,
) -> IngestResult:
    """Ingest one file, returning what happened rather than raising.

    Only `ModelMismatch` escapes: that is a fact about the whole run, and
    continuing past it would fill the database with incomparable vectors.
    """
    path = Path(path)
    started = time.perf_counter()
    # One trace per file, so every line the parser, the chunker, the embedder
    # and the HTTP retries emit can be pulled out of app.jsonl together.
    with trace_context(), span("ingest.file", log, path=str(path)) as bag:
        try:
            result = _ingest_one(
                path,
                conn,
                embedder,
                settings or get_settings(),
                force=force,
                dry_run=dry_run,
                describe_figures=describe_figures,
                started=started,
            )
        except ModelMismatch:
            raise
        except Exception as exc:
            log.exception("ingest.failed", extra={"path": str(path)})
            bag["status"] = "error"
            return IngestResult(
                path=path,
                status="failed",
                elapsed=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        bag.update(
            status=result.status,
            chunks=result.chunks,
            pages=result.pages,
            spine_source=result.spine_source,
        )
        return result


def _ingest_one(
    path: Path,
    conn: sqlite3.Connection | None,
    embedder: Embedder | None,
    settings: Settings,
    *,
    force: bool,
    dry_run: bool,
    describe_figures: bool,
    started: float,
) -> IngestResult:
    if not path.exists():
        raise FileNotFoundError(path)

    if dry_run:
        # Parse and chunk, so the report is real, but touch neither the API nor
        # the database. This is how chunking gets tuned before spending money.
        prepared = _prepare(path, settings, describe_figures)
        return _result(path, "dry-run", None, prepared, started)

    assert conn is not None and embedder is not None  # ingest_paths checked
    check_embedding_model(conn, embedder)

    relative = _relative(path)
    existing = conn.execute(
        "SELECT id, content_hash FROM documents WHERE path = ?", (relative,)
    ).fetchone()
    digest = file_hash(path)

    if existing is not None and existing["content_hash"] == digest and not force:
        # Zero API calls, and -- the expensive part -- no parse.
        chunks = conn.execute(
            "SELECT count(*) FROM chunks WHERE document_id = ?", (existing["id"],)
        ).fetchone()[0]
        row = conn.execute(
            "SELECT spine_source FROM documents WHERE id = ?", (existing["id"],)
        ).fetchone()
        return IngestResult(
            path=path,
            status="skipped",
            document_id=existing["id"],
            chunks=chunks,
            spine_source=row["spine_source"] if row else "none",
            elapsed=time.perf_counter() - started,
        )

    if existing is not None:
        # The parser writes into this directory and de-duplicates by content
        # hash within a run, not across runs: without the clear, a figure that
        # moved or vanished in the new version leaves an orphan file that a
        # stale `asset_path` could still cite.
        _clear_assets(path, settings)

    prepared = _prepare(path, settings, describe_figures)

    with span("ingest.embed", log, chunks=len(prepared.chunks)) as bag:
        vectors = embedder.embed_documents([chunk.content for chunk in prepared.chunks])
        bag["model"] = embedder.name
        # Captured, never tiled. At $0.02/1M, embedding the 21-page sample is
        # about $0.0002 against the ~$0.96 analysis it enables -- four orders
        # of magnitude, so a dashboard tile would be four leading zeros. What
        # it buys is the sentence the waterfall can then make: ingestion costs
        # a fiftieth of a cent and the dollar is all reasoning. `getattr`
        # because `Embedder` is a protocol and only `BaseEmbedder` promises
        # the attribute; the local and fake embedders truthfully report zero.
        bag["tokens"] = tokens = int(getattr(embedder, "last_tokens", 0) or 0)
        bag["cost_usd"] = embedding_cost_usd(embedder.name, tokens)

    with span("ingest.write", log, chunks=len(prepared.chunks)):
        document_id = _write(
            conn,
            path=path,
            relative=relative,
            digest=digest,
            prepared=prepared,
            vectors=vectors,
            embedder=embedder,
            replacing=None if existing is None else existing["id"],
        )
    status: IngestStatus = "replaced" if existing is not None else "ingested"
    return _result(path, status, document_id, prepared, started)


# --------------------------------------------------------------------------
# The stages
# --------------------------------------------------------------------------


def _prepare(path: Path, settings: Settings, describe_figures: bool) -> _Prepared:
    with span("ingest.parse", log, path=str(path)) as bag:
        parsed = parse_pdf(path, assets_dir=settings.assets_dir)
        bag.update(
            pages=parsed.page_count,
            elements=len(parsed.elements),
            spine_source=parsed.spine_source,
            sections=len(parsed.sections),
        )
    if describe_figures and parsed.figures:
        _describe(parsed, settings)
    report = ChunkingReport()
    with span("ingest.chunk", log) as bag:
        chunks = chunk_document(parsed, settings, report=report)
        bag.update(chunks=len(chunks), **report.as_dict())
    return _Prepared(parsed=parsed, chunks=chunks, report=report)


def _describe(parsed: ParsedDocument, settings: Settings) -> None:
    """The optional vision pass. Never fatal: a description is a nice-to-have."""
    from ..parse.describe import DescriptionUnavailable, describe_figures

    try:
        written = describe_figures(parsed.figures, settings=settings)
        log.info(
            "ingest.described",
            extra={"written": written, "figures": len(parsed.figures), "file": parsed.path.name},
        )
    except DescriptionUnavailable as exc:
        log.warning(
            "ingest.describe_skipped", extra={"file": parsed.path.name, "reason": str(exc)}
        )


def _write(
    conn: sqlite3.Connection,
    *,
    path: Path,
    relative: str,
    digest: str,
    prepared: _Prepared,
    vectors: list[list[float]],
    embedder: Embedder,
    replacing: int | None,
) -> int:
    """Delete the old document and write the new one in a single transaction.

    Nothing here is allowed to leave a half-ingested file behind: a `documents`
    row without its chunks would be reported as ingested and answer nothing,
    and chunks without vectors would be invisible to KNN but visible to BM25.
    `with conn` commits on success and rolls back on any exception.
    """
    import sqlite_vec

    if len(vectors) != len(prepared.chunks):
        raise RuntimeError(
            f"{len(vectors)} vectors for {len(prepared.chunks)} chunks in {path.name}"
        )

    parsed = prepared.parsed
    with conn:
        if replacing is not None:
            # ON DELETE CASCADE clears `chunks`; the chunks_ad trigger clears
            # `chunks_vec` and retracts each row from `chunks_fts`.
            conn.execute("DELETE FROM documents WHERE id = ?", (replacing,))

        cursor = conn.execute(
            """INSERT INTO documents (path, filename, content_hash, page_count,
                                      producer, has_outline, spine_source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                relative,
                path.name,
                digest,
                parsed.page_count,
                parsed.producer or None,
                int(parsed.has_outline),
                parsed.spine_source,
            ),
        )
        document_id = int(cursor.lastrowid or 0)

        conn.executemany(
            """INSERT INTO chunks (document_id, ordinal, content, page, page_label,
                                   page_end, page_label_end, section, section_path,
                                   element_type, bbox, asset_path, payload,
                                   token_count, embedding_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [_chunk_row(document_id, chunk, embedder.name) for chunk in prepared.chunks],
        )

        if prepared.chunks:
            # `executemany` cannot hand back the ids it generated, so read them
            # in the order the chunks were written. `ordinal` is dense and
            # unique per document, so this zips exactly.
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM chunks WHERE document_id = ? ORDER BY ordinal",
                    (document_id,),
                )
            ]
            if len(ids) != len(vectors):
                raise RuntimeError(f"{len(ids)} chunk rows for {len(vectors)} vectors")
            # `document_id` is the vec0 partition key. It is not optional in
            # practice: a row written without it lands in the NULL partition,
            # where every document-scoped KNN query will fail to see it while
            # the row counts still tally.
            conn.executemany(
                "INSERT INTO chunks_vec (chunk_id, document_id, embedding) VALUES (?, ?, ?)",
                [
                    (chunk_id, document_id, sqlite_vec.serialize_float32(vector))
                    for chunk_id, vector in zip(ids, vectors, strict=True)
                ],
            )
        # chunks_fts needs no write: the chunks_ai trigger has already filled it.
    return document_id


def _chunk_row(document_id: int, chunk: Chunk, model: str) -> tuple:
    """A `Chunk` as a `chunks` row.

    The fields that are not a plain scalar are the ones to get right:
    `section_path` and `bbox` become JSON strings, and `asset_path` was already
    made relative to the project root by the chunker so the database stays
    portable. `page_end` is NULL rather than a repeat of `page` when the chunk
    sits on one page, so "p.4" and "p.4-5" are distinguishable in SQL.
    """
    return (
        document_id,
        chunk.ordinal,
        chunk.content,
        chunk.page,
        chunk.page_label,
        chunk.page_end,
        chunk.page_label_end or None,
        chunk.section,
        json.dumps(chunk.section_path, ensure_ascii=False),
        chunk.element_type,
        json.dumps(list(chunk.bbox)) if chunk.bbox else None,
        chunk.asset_path,
        chunk.payload,
        chunk.token_count,
        model,
    )


def _clear_assets(path: Path, settings: Settings) -> None:
    """Remove a document's extracted figures before re-parsing it."""
    target = asset_dir(settings.assets_dir, path.stem)
    if target.is_dir():
        shutil.rmtree(target)


def _result(
    path: Path,
    status: IngestStatus,
    document_id: int | None,
    prepared: _Prepared,
    started: float,
) -> IngestResult:
    counts = [chunk.token_count for chunk in prepared.chunks]
    return IngestResult(
        path=path,
        status=status,
        document_id=document_id,
        pages=prepared.parsed.page_count,
        elements=len(prepared.parsed.elements),
        chunks=len(prepared.chunks),
        tokens=sum(counts),
        median_tokens=int(median(counts)) if counts else 0,
        max_tokens=max(counts, default=0),
        elapsed=time.perf_counter() - started,
        spine_source=prepared.parsed.spine_source,
        report=prepared.report,
    )


def _relative(path: Path) -> str:
    """A path relative to the project root, so the database is portable."""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


__all__ = [
    "KNOWN_SUFFIXES",
    "OPENAI_COST_PER_TOKEN",
    "IngestResult",
    "ModelMismatch",
    "check_embedding_model",
    "check_query_model",
    "collect_paths",
    "ingest_file",
    "ingest_paths",
    "stored_embedding_models",
]
