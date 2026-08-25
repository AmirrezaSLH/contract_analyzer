"""The Monitor tab's data. Stages from spans; host from the sampler."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ..deps import ConnDep, MetricsDep, Protected
from ..schemas import MonitorHost, MonitorStages

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


@router.get("/host", summary="Host memory and disk headroom")
def host(
    conn: ConnDep,
    store: MetricsDep,
    window: Annotated[str, Query(pattern=_SPEC)] = "24h",
) -> MonitorHost:
    """How much RAM this process is using, and how full the disk is.

    Tiles are the latest sample. The series follows `window`. No pager: the
    chips say plenty / filling / tight.
    """
    return MonitorHost.model_validate(store.host(conn, window=window))
