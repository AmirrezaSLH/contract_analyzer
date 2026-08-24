"""One protocol, three backends, and the guard that keeps them apart.

An embedder turns text into a vector. Which one produced a given vector is not
a detail: two models embedding the same clause give two unrelated points, and
mixing them produces rankings that look plausible and are noise. So every
implementation carries a `name`, that name is written to `chunks.embedding_model`
on every row, and the pipeline refuses to add to a corpus embedded by a
different one.

The second thing this file enforces is width. `config.py` checks *intent* --
that the configured width is one the provider can emit. `BaseEmbedder` checks
*reality*: the first vector that comes back is measured, because a `vec0` table
rejects a wrong-width vector with an error that says nothing about which model
sent it, and only after a contract has already been parsed and paid for.

Documents and queries take separate entry points rather than one `embed()`,
because some models want them treated differently -- bge prefixes a query with
an instruction it must never prepend to a passage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..config import Settings, get_settings
from ..logger import get_logger

log = get_logger(__name__)


class EmbedderUnavailable(RuntimeError):
    """The configured provider cannot run: no API key, or a missing package."""


class DimensionMismatch(RuntimeError):
    """A provider returned vectors of a width the database cannot store."""


@runtime_checkable
class Embedder(Protocol):
    """What the pipeline and retrieval both depend on."""

    #: Goes into `chunks.embedding_model`, verbatim.
    name: str
    #: Vector width, which the `chunks_vec` table fixes at creation time.
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class BaseEmbedder(ABC):
    """Shared plumbing: the width check, and the document/query split.

    Subclasses implement `_embed`, which receives the whole list and a flag for
    which side of the asymmetry it is on. Batching belongs to the subclass,
    since what a batch costs differs by two orders of magnitude between an HTTP
    round trip and a local forward pass.
    """

    name: str
    dim: int

    def __init__(self, name: str, dim: int) -> None:
        self.name = name
        self.dim = dim
        self._verified = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._embed(texts, query=False)
        if len(vectors) != len(texts):
            raise DimensionMismatch(
                f"{self.name} returned {len(vectors)} vectors for {len(texts)} texts"
            )
        self._verify(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vector = self._embed([text], query=True)[0]
        self._verify(vector)
        return vector

    @abstractmethod
    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        """Embed `texts`. `query` selects the asymmetric treatment, if any."""

    def _verify(self, vector: list[float]) -> None:
        """Measure the first vector we ever see; after that, trust the model."""
        if self._verified:
            return
        if len(vector) != self.dim:
            raise DimensionMismatch(
                f"{self.name} returned {len(vector)}-dim vectors but EMBEDDING_DIM="
                f"{self.dim}. The vec0 table's width is fixed at creation, so set "
                "EMBEDDING_DIM to match and rebuild the database."
            )
        self._verified = True

    def __repr__(self) -> str:  # what the ingest report prints
        return f"{type(self).__name__}(name={self.name!r}, dim={self.dim})"


def normalize(vector: list[float]) -> list[float]:
    """Scale to unit length, so L2 distance and cosine similarity agree.

    `vec0` ranks by L2. On unit vectors the two orderings are identical
    (||a-b||^2 = 2 - 2*cos), which is what everyone reading a similarity score
    assumes. A zero vector -- an empty or unrecognised input -- is returned
    unchanged rather than turned into NaNs.
    """
    total = sum(value * value for value in vector) ** 0.5
    if total == 0.0:
        return list(vector)
    return [value / total for value in vector]


def _build_openai(settings: Settings) -> Embedder:
    from .openai import OpenAIEmbedder

    return OpenAIEmbedder(settings)


def _build_local(settings: Settings) -> Embedder:
    from .local import LocalEmbedder

    return LocalEmbedder(settings)


def _build_fake(settings: Settings) -> Embedder:
    from .fake import FakeEmbedder

    return FakeEmbedder(settings)


#: Imports are deferred into the factories so that importing this package costs
#: nothing and, more to the point, so it stays importable without the ~800 MB
#: `[local]` extra installed.
_REGISTRY: dict[str, Callable[[Settings], Embedder]] = {
    "openai": _build_openai,
    "local": _build_local,
    "fake": _build_fake,
}


def get_embedder(settings: Settings | None = None) -> Embedder:
    """The embedder named by `EMBEDDING_PROVIDER`."""
    settings = settings or get_settings()
    settings.validate_embedding_dim()
    try:
        build = _REGISTRY[settings.embedding_provider]
    except KeyError:  # pragma: no cover - pydantic rejects unknown values first
        raise EmbedderUnavailable(
            f"unknown EMBEDDING_PROVIDER={settings.embedding_provider!r}; "
            f"expected one of {', '.join(sorted(_REGISTRY))}"
        ) from None
    embedder = build(settings)
    log.debug("embedder.selected", extra={"embedder": repr(embedder)})
    return embedder


__all__ = [
    "BaseEmbedder",
    "DimensionMismatch",
    "Embedder",
    "EmbedderUnavailable",
    "get_embedder",
    "normalize",
]
