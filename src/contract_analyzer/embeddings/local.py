"""A local embedder: `BAAI/bge-small-en-v1.5`, 384 dimensions, no network.

The reason this exists is not cost -- embedding a whole contract with OpenAI is
a hundredth of a cent. It is that a demo has to survive the room's wifi. With
`pip install -e ".[local]"` and the model cached, ingestion and retrieval run
with no key and no connection at all, and unlike `fake` the vectors mean
something.

The price is an ~800 MB `torch` install, so the import is deferred to the
constructor: the package must stay importable, and `pytest` must stay
runnable, without the extra.

bge is an **asymmetric** model: it was trained with an instruction prefix on
the query side only. Prepending it to passages as well does not "keep things
consistent", it moves every document toward the same region and flattens the
ranking -- so the prefix lives in `embed_query` and appears nowhere else.
"""

from __future__ import annotations

from ..config import Settings
from .base import BaseEmbedder, EmbedderUnavailable

#: bge's training-time query instruction. Queries only -- see the module note.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

#: Sentences per forward pass. Small because this may be running on a CPU.
BATCH_SIZE = 32


class LocalEmbedder(BaseEmbedder):
    def __init__(self, settings: Settings) -> None:
        super().__init__(name=settings.resolved_embedding_model, dim=settings.embedding_dim)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbedderUnavailable(
                "EMBEDDING_PROVIDER=local needs the optional extra: "
                'pip install -e ".[local]"'
            ) from exc
        # First call downloads the weights (~130 MB) to the HF cache; after
        # that it is offline.
        self._model = SentenceTransformer(self.name)

    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        payload = [QUERY_PREFIX + text for text in texts] if query else list(texts)
        vectors = self._model.encode(
            payload,
            batch_size=BATCH_SIZE,
            # The model does this better than we can afterwards, and it makes
            # `vec0`'s L2 ranking equal to cosine ranking.
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]


__all__ = ["BATCH_SIZE", "QUERY_PREFIX", "LocalEmbedder"]
