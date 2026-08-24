"""Ingestion: a file on disk becomes rows in the four tables.

`chunker.py` decides what a retrieval unit is; `pipeline.py` runs the whole
path -- parse, chunk, embed, store -- and is the part that has to be idempotent.
"""

from .chunker import ChunkingReport, chunk_document, chunk_elements
from .pipeline import (
    IngestResult,
    ModelMismatch,
    check_embedding_model,
    collect_paths,
    ingest_file,
    ingest_paths,
)

__all__ = [
    "ChunkingReport",
    "IngestResult",
    "ModelMismatch",
    "check_embedding_model",
    "chunk_document",
    "chunk_elements",
    "collect_paths",
    "ingest_file",
    "ingest_paths",
]
