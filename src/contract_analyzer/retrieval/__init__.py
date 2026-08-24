"""Retrieval: two retrievers over one database, fused, scoped to one contract.

`chunks_vec` answers "what does this mean", `chunks_fts` answers "who says
these exact words", and `hybrid.retrieve()` fuses the two rankings with
Reciprocal Rank Fusion. `sections.retrieve_by_section()` is the third way in --
no embedder, no ranking, just "give me 6.6" -- for callers that know the
structure they want.

Every search takes a `document_id`, because an analysis is about one uploaded
contract; `ALL_DOCUMENTS` is how a corpus-wide search says so out loud.
"""

from .base import (
    ALL_DOCUMENTS,
    RetrievalResult,
    RetrievedChunk,
    hydrate,
    similarity_from_distance,
)
from .hybrid import NEEDS_EMBEDDER, retrieve, rrf_fuse
from .keyword import escape_query, keyword_search
from .sections import retrieve_by_section
from .vector import embed_question, vector_search

__all__ = [
    "ALL_DOCUMENTS",
    "NEEDS_EMBEDDER",
    "RetrievalResult",
    "RetrievedChunk",
    "embed_question",
    "escape_query",
    "hydrate",
    "keyword_search",
    "retrieve",
    "retrieve_by_section",
    "rrf_fuse",
    "similarity_from_distance",
    "vector_search",
]
