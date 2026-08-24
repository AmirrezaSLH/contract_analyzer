"""OpenAI embeddings: `text-embedding-3-small`, truncated to 512 dimensions.

The default. A 21-page contract is about 10k tokens of chunk text, so at
$0.02/1M ingesting one costs a fiftieth of a cent -- the interesting numbers
here are not the money.

Three things are not boilerplate.

**Truncation.** The `dimensions` parameter asks the API for a Matryoshka
prefix of the full 1536-dim vector -- the model is trained so that the first N
components are themselves a usable embedding. 512 costs a little accuracy and
cuts a brute-force KNN scan to a third of the work.

**Re-normalisation.** OpenAI's full-width vectors are unit length; a prefix of
a unit vector is not. `vec0` ranks by L2 distance, which only agrees with
cosine similarity on unit vectors -- so without the two lines in `normalize`
the ranking silently drifts toward whichever chunks happen to have the largest
prefix norm. Nothing errors. The results just get worse.

**No retry loop.** The SDK is built with `max_retries=0` on the project's own
`httpx` client, so the single retry policy in `http_client.py` is the one that
runs: one place that decides what is retryable, one place that logs
`http.retry`, and no chance of two backoff loops multiplying into sixteen
requests. When that policy is exhausted the SDK wraps the failure in an
`APIConnectionError`; it is unwrapped here so the caller sees the one-line
`HttpFailure` naming the URL, the attempts and the elapsed time.

(Importing the `openai` package from a module of this name is safe: Python 3
resolves `import openai` to the installed top-level package, never the sibling.)
"""

from __future__ import annotations

from ..config import Settings
from ..http_client import HttpFailure, get_http_client
from ..logger import get_logger
from .base import BaseEmbedder, EmbedderUnavailable, normalize

log = get_logger(__name__)

#: Texts per request. The API's ceiling is far higher, but a chunk here runs to
#: 400 tokens and 100 of them is a comfortable payload that still puts a whole
#: contract in a single round trip.
BATCH_SIZE = 100

#: Only the `-3` family accepts `dimensions`; `ada-002` returns 400 if it is
#: sent. Overridable via EMBEDDING_MODEL, so this is checked rather than assumed.
_TRUNCATABLE_PREFIX = "text-embedding-3"


class OpenAIEmbedder(BaseEmbedder):
    def __init__(self, settings: Settings) -> None:
        super().__init__(name=settings.resolved_embedding_model, dim=settings.embedding_dim)
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise EmbedderUnavailable("the `openai` package is not installed") from exc

        if not settings.openai_key:
            raise EmbedderUnavailable(
                "OPENAI_API_KEY is not set. Set it in .env, switch "
                "EMBEDDING_PROVIDER to `local` for real offline vectors, or to "
                "`fake` to run the pipeline with no key at all."
            )
        self._openai = openai
        self._client = openai.OpenAI(
            api_key=settings.openai_key,
            # The project's transport, and its retry policy, not the SDK's.
            http_client=get_http_client(settings),
            max_retries=0,
        )
        self._truncatable = self.name.startswith(_TRUNCATABLE_PREFIX)
        if not self._truncatable:
            log.warning(
                "embedder.no_truncation",
                extra={
                    "model": self.name,
                    "dim": self.dim,
                    "detail": "model does not accept `dimensions`; the returned "
                    "width will be checked against EMBEDDING_DIM instead",
                },
            )

    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        # The API rejects an empty string, and a chunk should never be one --
        # but a single bad row must not fail a contract, so substitute a space.
        payload = [text if text.strip() else " " for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(payload), BATCH_SIZE):
            batch = payload[start : start + BATCH_SIZE]
            response = self._request(batch)
            # `data` is documented as index-ordered; sorting makes that
            # explicit rather than load-bearing and unstated.
            for item in sorted(response.data, key=lambda d: d.index):
                vectors.append(normalize(list(item.embedding)))
        return vectors

    def _request(self, batch: list[str]):
        """One call. Retries happen in the transport, below this frame."""
        kwargs: dict[str, object] = {"model": self.name, "input": batch}
        if self._truncatable:
            kwargs["dimensions"] = self.dim
        try:
            return self._client.embeddings.create(**kwargs)
        except self._openai.AuthenticationError as exc:
            # Not retryable and not a transport problem: it is a configuration
            # error, and it should read like one.
            raise EmbedderUnavailable(
                f"OpenAI rejected the API key for {self.name}. Check OPENAI_API_KEY, "
                "or set EMBEDDING_PROVIDER=fake to run without one."
            ) from exc
        except self._openai.APIConnectionError as exc:
            # The SDK wraps whatever the transport raised. When that was our
            # own exhausted retry policy, the caller wants that message -- it
            # names the URL, the attempt count and the elapsed time -- and not
            # a generic connection error two layers above it.
            if isinstance(exc.__cause__, HttpFailure):
                raise exc.__cause__ from None
            raise


__all__ = ["BATCH_SIZE", "OpenAIEmbedder"]
