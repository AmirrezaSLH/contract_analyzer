"""Counting tokens, so the chunk budget means something.

`CHUNK_TOKENS=600` is only a real limit if the count is the one the embedder
will do. `len(text) // 4` is close enough for prose and badly wrong for the two
things this corpus is full of: a markdown table of numbers is roughly two
tokens per cell, and `E = tout*Cout + tv*Cv` is a dozen tokens in eight
characters of ink.

So: `tiktoken` when it is there, the ruler when it is not. The fallback is not
only for a missing package -- `tiktoken` fetches its BPE table over the network
the first time an encoding is used, which fails on an offline machine with a
cold cache. Both cases land in the same place, once, and the result is cached.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

#: The encoding behind `text-embedding-3-small` and every current Claude and
#: GPT model. A chunk budget does not need per-model precision, it needs to
#: stop being off by 40% on tables.
ENCODING_NAME = "cl100k_base"

#: Bytes per token in English prose, near enough. Only used when tiktoken is
#: unavailable; `//4` rounds a short line down to zero, so this rounds up.
_CHARS_PER_TOKEN = 4


@lru_cache(maxsize=1)
def _encoder() -> Any | None:
    """The tiktoken encoding, or None if it cannot be had. Resolved once."""
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding(ENCODING_NAME)
    except Exception:
        # The BPE table is downloaded on first use: no network, no encoder.
        return None


def count_tokens(text: str) -> int:
    """Tokens in `text` -- exact where possible, estimated where not."""
    encoder = _encoder()
    if encoder is None:
        return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
    # A document that happens to contain "<|endoftext|>" is text, not a control
    # sequence; without this tiktoken raises on it.
    return len(encoder.encode(text, disallowed_special=()))


def using_tiktoken() -> bool:
    """Whether counts are exact. The ingest report says which one it used."""
    return _encoder() is not None


__all__ = ["ENCODING_NAME", "count_tokens", "using_tiktoken"]
