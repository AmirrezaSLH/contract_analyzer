"""What a call cost, from the token counts the API reports.

A small table, USD per million tokens, for the models this project would
plausibly be pointed at. The KPI page's cost tile reads `cost_usd`; nothing
else does. Cache reads are billed at a tenth of the input rate and cache
writes at 1.25x, per the published rates.

An unknown model prices at zero and logs `pricing.unknown_model` once, rather
than raising: a cost tile that reads "$0.00 (unpriced)" is a better failure
than an analysis that refuses to run because a new model id was set in
`.env` before this table learned about it.
"""

from __future__ import annotations

from ..logger import get_logger

log = get_logger(__name__)

#: (input, output) USD per 1M tokens. Cached: 2026-06-24.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

CACHE_READ_FACTOR = 0.1
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


__all__ = ["CACHE_READ_FACTOR", "CACHE_WRITE_FACTOR", "PRICES", "cost_usd", "is_priced"]
