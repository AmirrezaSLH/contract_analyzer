"""Ingestion: a file on disk becomes rows in the four tables.

`chunker.py` decides what a retrieval unit is; `pipeline.py` runs the whole
path -- parse, chunk, embed, store -- and is the part that has to be idempotent.
"""

from .chunker import ChunkingReport, chunk_document, chunk_elements

__all__ = ["ChunkingReport", "chunk_document", "chunk_elements"]
