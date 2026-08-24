"""The KPI page's data, over the metrics store.

Four operations, and the split between them is the one from `01_findings.md`:
the server aggregates, the browser draws. `GET /analyses` is per document on
purpose, so a dashboard that spans every contract cannot be assembled from it
-- pulling thirty kilobytes of report per run into React to `reduce()` is the
wrong grain, and session state under-counts after a refresh.

* `summary` -- the tiles and the meters for one window.
* `timeseries` -- the same numbers per bucket, for the trend charts.
* `runs` -- the global runs table, each row carrying its trace id.
* `runs/{id}/spans` -- one run's span tree, for the waterfall.

**The window drives the bucket.** 24 h -> `1h`, 7 d -> `6h`, 30 d -> `1d`, and
`timeseries` applies that pairing when a caller sends only a window. Thirty
days of one-hour bars is 720 marks on a 900-pixel axis.

**Live counts are not table reads.** Active and queued come from `JobRunner`;
they are facts about this process and a table would be describing the last one.
`summary` merges them in, which is why it is the one handler that depends on
the runner as well as on the store.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from ..deps import ConnDep, MetricsDep, Protected, RunnerDep

router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Protected])

#: `24h`, `7d`, `1h`. Validated by the router so a typo is a 422 in this API's
#: error envelope rather than a ValueError from the query layer.
_SPEC = r"^\d+[hd]$"


@router.get("/summary", summary="Real-time tiles for the KPI page")
def summary(
    conn: ConnDep,
    store: MetricsDep,
    runner: RunnerDep,
    window: Annotated[str, Query(pattern=_SPEC)] = "24h",
) -> dict[str, Any]:
    """Failure rate, p50/p95 latency, spend, and the three quality meters.

    `live` is the only part of this payload that is not a query: workers busy,
    runs queued, and the document count `GET /health` also reports.
    """
    running, queued = runner.live
    payload = store.summary(conn, window=window)
    payload["live"] = {"running": running, "queued": queued, "active": running + queued}
    return payload


@router.get("/timeseries", summary="Historical trends, bucketed")
def timeseries(
    conn: ConnDep,
    store: MetricsDep,
    window: Annotated[str, Query(pattern=_SPEC)] = "7d",
    bucket: Annotated[str | None, Query(pattern=_SPEC)] = None,
) -> list[dict[str, Any]]:
    """One entry per bucket, oldest first. Buckets with no runs are present
    and zeroed: a chart that closes its gaps draws a busy night out of a quiet
    one. `bucket` defaults to the one the window is designed with."""
    return store.timeseries(conn, window=window, bucket=bucket)


@router.get("/runs", summary="The runs table")
def runs(
    conn: ConnDep,
    store: MetricsDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    """Every run, newest first, whatever contract it was against. The report
    is not included -- open the analysis for that."""
    return store.runs(conn, limit=limit)


@router.get("/runs/{run_id}/spans", summary="One run's span tree, for the waterfall")
def spans(conn: ConnDep, store: MetricsDep, run_id: str) -> list[dict[str, Any]]:
    """Every span of one run, as a tree: `api.analysis` -> `analysis.document`
    -> one `analysis.criterion` per criterion -> `agent.run` -> `agent.call` /
    `agent.tool` -> `retrieve`.

    A tree rather than a flat list, because resolving `parent_span_id` in the
    browser would mean writing the same algorithm again in TypeScript. An
    empty list is the honest answer for a run with no spans -- one from before
    this table existed, or from another machine -- and not a 404: the run may
    well be in `/metrics/runs` beside it.
    """
    return store.spans(conn, run_id)
