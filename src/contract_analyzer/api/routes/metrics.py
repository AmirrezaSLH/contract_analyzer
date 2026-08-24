"""The KPI page's data, over the metrics store.

The store does not exist yet -- `spans`, `criterion_results` and the query
layer over them are the next step -- so these four endpoints are declared and
answer `503 metrics_unavailable`. The `analyses` table they will join against
is already there and already populated, by `analyses.py`; what is missing is
the per-criterion and per-span detail, and the percentile queries.

The endpoints are here rather than absent for one reason: the
OpenAPI document is a deliverable, the connector and the UI are written against
it, and an endpoint that is documented and honestly unavailable is a better
contract than one that appears later and changes the shape of the spec.

When `MetricsStore` lands, each handler becomes a call to
`summary` / `timeseries` / `runs` / `spans` and returns what it returns,
unchanged: the KPI selection is the KPI plan's business, not this module's.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ..deps import Protected, RunnerDep
from ..errors import ApiError

router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Protected])

_HINT = (
    "The metrics store is not implemented yet. GET /health reports live counts, "
    "GET /analyses?document_id=... lists one contract's runs, and GET /analyses/{id} "
    "carries the totals for one of them."
)


def _unavailable() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "metrics_unavailable",
        "Historical metrics are not available: the metrics store has not been built yet.",
        _HINT,
    )


@router.get("/summary", summary="Real-time tiles for the KPI page")
def summary(runner: RunnerDep, window: Annotated[str, Query()] = "24h") -> dict[str, Any]:
    raise _unavailable()


@router.get("/timeseries", summary="Historical trends, bucketed")
def timeseries(
    bucket: Annotated[str, Query()] = "1h",
    window: Annotated[str, Query()] = "7d",
) -> list[dict[str, Any]]:
    raise _unavailable()


@router.get("/runs", summary="The runs table")
def runs(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:
    raise _unavailable()


@router.get("/runs/{run_id}/spans", summary="One run's span tree, for the waterfall")
def spans(run_id: str) -> list[dict[str, Any]]:
    raise _unavailable()
