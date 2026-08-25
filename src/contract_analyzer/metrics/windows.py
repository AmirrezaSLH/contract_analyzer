"""Windows, buckets, and the arithmetic that keeps a chart's axis continuous.

The UI has one control -- 24 hours / 7 days / 30 days -- and it drives both
query parameters, because they are not independent: 30 days of one-hour bars
is 720 marks on an axis 900 pixels wide. `DEFAULT_BUCKETS` is that pairing,
and it is here rather than in the front end so that the API and the design
cannot drift.

Two things a caller would otherwise get wrong:

* **Buckets are epoch-aligned, not now-aligned.** A bucket is
  `floor(unixepoch / size)`, so the same run lands in the same bucket whoever
  asks and whenever they ask. Bucketing relative to the request time would
  move every bar each time the page refreshed.
* **Empty buckets are still buckets.** `GROUP BY` cannot return a row for an
  hour in which nothing happened, and a chart that silently closes those gaps
  draws a busy night out of a quiet one. `bucket_starts` enumerates the whole
  axis and the query layer fills what SQL returned into it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

#: The window selector's pairs. 24 h -> 24 bars, 7 d -> 28, 30 d -> 30.
DEFAULT_BUCKETS: dict[str, str] = {"24h": "1h", "7d": "6h", "30d": "1d"}

_UNITS = {"m": 60, "h": 3600, "d": 86400}
_SPEC = re.compile(r"^(\d+)([mhd])$")

#: How a bucket start is spelled on the wire. `Z` rather than `+00:00` because
#: it is a label a chart prints, not a value SQLite has to parse back.
_LABEL = "%Y-%m-%dT%H:%M:%SZ"


def seconds(spec: str) -> int:
    """`24h` -> 86400, `5m` -> 300. Raises ValueError on anything else."""
    match = _SPEC.match(spec.strip().lower())
    if match is None or int(match.group(1)) < 1:
        raise ValueError(f"expected a window like 24h, 7d, 1h or 5m, got {spec!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def bucket_for(window: str) -> str:
    """The bucket this window is drawn with. Unknown windows get `1h`."""
    return DEFAULT_BUCKETS.get(window.strip().lower(), "1h")


def since(window: str, *, now: datetime | None = None) -> str:
    """The lower bound of the window, spelled the way `created_at` is.

    Same format, so the comparison is a string compare on an indexed column
    rather than a per-row `datetime()` call: every timestamp this project
    writes is UTC ISO-8601 with an explicit offset, which sorts correctly as
    text exactly because it is fixed-width and zero-padded.
    """
    moment = (now or datetime.now(UTC)) - timedelta(seconds=seconds(window))
    return moment.isoformat(timespec="seconds")


def bucket_starts(window: str, bucket: str, *, now: datetime | None = None) -> list[str]:
    """Every bucket label in the window, oldest first, gaps included."""
    size = seconds(bucket)
    end = int((now or datetime.now(UTC)).timestamp())
    first = ((end - seconds(window)) // size) * size
    last = (end // size) * size
    return [label(moment) for moment in range(first, last + 1, size)]


def label(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime(_LABEL)


def bucket_expression(column: str, bucket: str) -> str:
    """SQL that floors `column` to the start of its bucket.

    Interpolated rather than bound because a bucket size is not a value in an
    expression SQLite will accept as a parameter here -- and it is an integer
    this module derived from a regex, never a caller's string.
    """
    size = seconds(bucket)
    return (
        f"strftime('{_LABEL}', "
        f"(CAST(strftime('%s', {column}) AS INTEGER) / {size}) * {size}, 'unixepoch')"
    )


__all__ = [
    "DEFAULT_BUCKETS",
    "bucket_expression",
    "bucket_for",
    "bucket_starts",
    "label",
    "seconds",
    "since",
]
