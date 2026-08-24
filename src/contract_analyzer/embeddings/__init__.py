"""Text becomes vectors. One protocol, three interchangeable backends.

`base.py` holds the protocol, the width check and the registry; `guard.py`
holds the rule that keeps two models out of one corpus. The provider modules
are imported only when selected, so neither the `[local]` extra nor an API key
is needed to import this package.
"""

from .base import (
    BaseEmbedder,
    DimensionMismatch,
    Embedder,
    EmbedderUnavailable,
    get_embedder,
    normalize,
)
from .guard import (
    ModelMismatch,
    check_embedding_model,
    check_query_model,
    stored_embedding_models,
)

__all__ = [
    "BaseEmbedder",
    "DimensionMismatch",
    "Embedder",
    "EmbedderUnavailable",
    "ModelMismatch",
    "check_embedding_model",
    "check_query_model",
    "get_embedder",
    "normalize",
    "stored_embedding_models",
]
