"""A deterministic offline embedder, for tests and for a keyless demo.

Hash each word to a coordinate, add one there, normalise. That is a bag of
words projected onto `dim` axes -- it has **no semantics whatsoever**. Two
paraphrases that share no vocabulary are orthogonal under it, so "secure admin
pathway" retrieves nothing for "bastion host". It is not a retrieval strategy
and must never be mistaken for one, which is why its `name` is `fake-hash-512`
and lands in `chunks.embedding_model` on every row it writes: a database built
with it announces itself, and the model guard refuses to add real vectors to it.

What it is for: `pytest` never touching the network, and the whole ingestion
path being demonstrable end to end with no API key. Offline, keyword mode is
the one with real signal -- which on a contract full of `PASS-02` and
`TLS 1.2` is further than it sounds.

Hashing is `blake2b`, not the builtin `hash()`, which is randomised per process
by PYTHONHASHSEED -- the same text must give the same vector tomorrow, or
"re-ingesting is a no-op" stops being testable.
"""

from __future__ import annotations

import hashlib
import re

from ..config import Settings
from .base import BaseEmbedder, normalize

_WORD = re.compile(r"\w+", re.UNICODE)


class FakeEmbedder(BaseEmbedder):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            # The width is part of the name: two fake databases of different
            # widths are as incompatible as two real models.
            name=f"{settings.resolved_embedding_model}-{settings.embedding_dim}",
            dim=settings.embedding_dim,
        )
        #: How many texts have been embedded. The ingest suite asserts this
        #: stays put when a file is skipped -- the cheapest possible proof that
        #: idempotency saves the API call and not merely the write.
        self.calls = 0

    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        self.calls += len(texts)
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "big") % self.dim] += 1.0
        return normalize(vector)


__all__ = ["FakeEmbedder"]
