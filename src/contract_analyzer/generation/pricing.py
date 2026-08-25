"""What a call cost, from the token counts the API reports.

Two small tables, USD per million tokens, for the models this project would
plausibly be pointed at: generation, where a rate is a pair because output
costs more than input, and embeddings, where it is one number because there is
no output. The KPI page's cost tile reads `cost_usd`; nothing else does. Cache
reads are billed at a tenth of the input rate and cache writes at 1.25x, per
the published rates.

An unknown model prices at zero and logs `pricing.unknown_model` once, rather
than raising: a cost tile that reads "$0.00 (unpriced)" is a better failure
than an analysis that refuses to run because a new model id was set in
`.env` before this table learned about it.
"""

from __future__ import annotations

from ..logger import get_logger

log = get_logger(__name__)

#: (input, output) USD per 1M tokens. Cached: 2026-08-24.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    # The launch price became the standard price; the announced rise to
    # $3/$15 on 2026-09-01 was cancelled.
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}

#: USD per 1M tokens for embedding models. Separate from PRICES because an
#: embedding call has no output tokens to price -- one rate, not a pair.
#:
#: This is here rather than in `embeddings/` for the reason `02_costs.md` gives:
#: one price table, one place to correct when a rate moves. The number it
#: produces is four orders of magnitude under the analysis it enables --
#: embedding the 21-page sample costs about $0.0002 against a ~$0.96 run -- so
#: it is captured and never tiled. It buys the waterfall's one honest sentence:
#: ingestion costs a fiftieth of a cent, and the dollar is all reasoning.
EMBEDDING_PRICES: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}

CACHE_READ_FACTOR = 0.1
#: For the 5-minute cache, the only TTL this project requests. A 1-hour
#: cache write bills at 2x and would need its own factor.
CACHE_WRITE_FACTOR = 1.25

_warned: set[str] = set()


def is_priced(model: str) -> bool:
    return model in PRICES


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollars for one call, or for a sum of calls on the same model."""
    rates = PRICES.get(model)
    if rates is None:
        if model not in _warned:
            _warned.add(model)
            log.warning("pricing.unknown_model", extra={"model": model})
        return 0.0
    rate_in, rate_out = rates
    dollars = (
        input_tokens * rate_in
        + cache_read_tokens * rate_in * CACHE_READ_FACTOR
        + cache_write_tokens * rate_in * CACHE_WRITE_FACTOR
        + output_tokens * rate_out
    ) / 1_000_000
    return round(dollars, 6)


def embedding_cost_usd(model: str, tokens: int) -> float:
    """Dollars for `tokens` embedded by `model`.

    Zero tokens prices at zero **without a warning**, which is the honest
    answer for the local and fake embedders: they report no usage because they
    bill none, and warning about an unpriced model every time somebody ingests
    offline would be noise about a number that is genuinely $0.00.
    """
    if tokens <= 0:
        return 0.0
    rate = EMBEDDING_PRICES.get(model)
    if rate is None:
        if model not in _warned:
            _warned.add(model)
            log.warning("pricing.unknown_embedding_model", extra={"model": model})
        return 0.0
    return round(tokens * rate / 1_000_000, 8)


__all__ = [
    "CACHE_READ_FACTOR",
    "CACHE_WRITE_FACTOR",
    "EMBEDDING_PRICES",
    "PRICES",
    "cost_usd",
    "embedding_cost_usd",
    "is_priced",
]
