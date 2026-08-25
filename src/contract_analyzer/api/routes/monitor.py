"""The Monitor tab's data. Stages first: a query over spans, no new capture."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ..deps import ConnDep, MetricsDep, Protected
from ..schemas import MonitorStages

router = APIRouter(prefix="/monitor", tags=["monitor"], dependencies=[Protected])

_SPEC = r"^\d+[hd]$"


@router.get("/stages", summary="Worst pipeline stage, and its trend")
def stages(
    conn: ConnDep,
    store: MetricsDep,
    window: Annotated[str, Query(pattern=_SPEC)] = "24h",
) -> MonitorStages:
    """Where it broke, not whether a run failed.

    Tiles are the last five minutes; the series follows `window`. `errors_total`
    counts every named stage, not only the worst. A stage with fewer than
    `min_samples` hits is reported with its n so 1-of-1 is not a 100% error rate.
    """
    return MonitorStages.model_validate(store.stages(conn, window=window))
