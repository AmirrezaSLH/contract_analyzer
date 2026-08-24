"""The Anthropic client on the project's transport, and the usage it reports.

Three things, none of them clever:

**One client, one retry loop.** `get_client()` builds `anthropic.Anthropic`
on `get_http_client()` with `max_retries=0`, exactly as the embedder does, so
the retry policy in `http_client.py` is the only one that runs and the only
one that logs.

**Errors mapped, not wrapped.** `api_errors()` turns an
`AuthenticationError` into `AnswerUnavailable` -- a configuration problem
that should read like one -- and unwraps the `HttpFailure` the SDK hides
inside `APIConnectionError`. A `BadRequestError` propagates untouched: a 400
is a bug in the request this package built, and the API's message names the
field.

**Usage summed.** Every call reports its tokens; `Usage` adds them across
the turns of one run so a result can say what it cost.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..http_client import get_http_client, unwrap_http_failure
from .pricing import cost_usd


class AnswerUnavailable(RuntimeError):
    """The answer model cannot be called: no key, or a key the API rejected."""


def get_client(settings: Settings | None = None) -> Any:
    """`anthropic.Anthropic` on the shared transport. Raises before any request."""
    settings = settings or get_settings()
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise AnswerUnavailable("the `anthropic` package is not installed") from exc
    if not settings.anthropic_key:
        raise AnswerUnavailable(
            "ANTHROPIC_API_KEY is not set. Put it in .env at the project root; "
            "retrieval (`make search`) works without it, generation does not."
        )
    return anthropic.Anthropic(
        api_key=settings.anthropic_key,
        http_client=get_http_client(settings),
        max_retries=0,
    )


@contextmanager
def api_errors() -> Iterator[None]:
    """Map the SDK's exceptions to the project's, around one request."""
    import anthropic

    try:
        yield
    except anthropic.AuthenticationError as exc:
        raise AnswerUnavailable(
            "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
        ) from exc
    except anthropic.APIConnectionError as exc:
        failure = unwrap_http_failure(exc)
        if failure is not None:
            raise failure from None
        raise


@dataclass(frozen=True)
class Usage:
    """Token counts, summable across the calls of one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_message(cls, message: Any) -> Usage:
        usage = getattr(message, "usage", None)
        if usage is None:
            return cls()
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )

    def cost(self, model: str) -> float:
        return cost_usd(
            model,
            self.input_tokens,
            self.output_tokens,
            cache_read_tokens=self.cache_read_input_tokens,
            cache_write_tokens=self.cache_creation_input_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


__all__ = ["AnswerUnavailable", "Usage", "api_errors", "get_client"]
