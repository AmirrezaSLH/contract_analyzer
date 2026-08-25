"""How every KPI number becomes a query.

`plan_implement_docs/KPI_01/Metric_Store.md` is the plan this implements, in
three phases:

1. **Queries over `analyses`** -- the whole of the dashboard's first cut, with
   no schema change: failure rate, p50/p95 latency, cost totals and trend,
   quote verification, needs-review, mean confidence, cap rate, runs count and
   the `surface` split. `queries.py` and `windows.py`.
2. **`spans`** -- one row per `span.end`, filed by a logging handler, which is
   what makes chat cost, cost per model and the per-run waterfall answerable
   without a line of change in any module that emits a span. `metrics.sql`,
   `handler.py`, and the `run_id` context variable in `logger.py`.
3. **`criterion_results`** -- state mix per criterion, the drift signal.

The direction of the imports is the load-bearing part: this package imports
`db` and `logger` and nothing above them, and nothing below it imports this
package. Storage must not depend on telemetry to record what happened.
"""

from __future__ import annotations

from .handler import SpanHandler
from .store import MetricsStore

__all__ = ["MetricsStore", "SpanHandler"]
