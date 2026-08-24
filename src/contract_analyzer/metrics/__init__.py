"""How every KPI number becomes a query.

`plan_implement_docs/KPI_01/Metric_Store.md` is the plan this implements, in
three phases:

1. **Queries over `analyses`** -- the whole of the dashboard's first cut, with
   no schema change: failure rate, p50/p95 latency, cost totals and trend,
   quote verification, needs-review, mean confidence, cap rate, runs count and
   the `surface` split. `queries.py` and `windows.py`.
2. **`spans`** -- one row per `span.end`, written by a logging handler, which
   makes chat cost, cost per model and the per-run waterfall answerable.
3. **`criterion_results`** -- state mix per criterion, the drift signal.

The direction of the imports is the load-bearing part: this package imports
`db` and `logger` and nothing above them, and nothing below it imports this
package. Storage must not depend on telemetry to record what happened.
"""

from __future__ import annotations

from .store import MetricsStore

__all__ = ["MetricsStore"]
