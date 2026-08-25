"""The Monitor tab's Upstream band: retries through http_client, not spend."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx2 as httpx
import pytest

from contract_analyzer import http_client as H
from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.logger import configure_logging
from contract_analyzer.metrics import MetricsStore
from contract_analyzer.metrics import upstream as U

NOW = datetime(2026, 8, 24, 12, 30, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=8,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path,
        assets_dir=tmp_path / "assets",
        log_file=None,
    )


@pytest.fixture
def store(settings) -> MetricsStore:
    made = MetricsStore(settings)
    yield made
    made.close()


@pytest.fixture
def conn(settings, store):
    return get_db(settings)


def put(
    conn,
    name: str,
    *,
    minutes_ago: float,
    reason: str | None = None,
    i: int = 0,
) -> None:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")
    attrs = json.dumps({"reason": reason}) if reason else None
    conn.execute(
        "INSERT INTO spans (span_id, name, status, ts, attrs) VALUES (?, ?, ?, ?, ?)",
        (f"{name}-{minutes_ago}-{i}-{reason}", name, "ok", ts, attrs),
    )
    conn.commit()


def test_empty_spans_are_nulls_not_a_failure(conn, store: MetricsStore):
    got = store.upstream(conn, window="30m")
    assert got["calls"] == 0
    assert got["retries_per_100"] is None
    assert got["exhausted_rate"] is None
    assert got["top_reason"] is None
    assert got["top_reason_share"] is None
    assert got["series"]
    assert all(
        row["calls"] == 0
        and row["retries_per_100"] is None
        and row["exhausted_rate"] is None
        for row in got["series"]
    )


def test_retries_per_100_and_exhausted_rate(conn):
    for i in range(10):
        put(conn, "upstream.call", minutes_ago=1, i=i)
    for i in range(4):
        put(conn, "upstream.retry", minutes_ago=1, reason="HTTP 429", i=i)
    for i in range(2):
        put(conn, "upstream.failed", minutes_ago=1, reason="HTTP 429", i=i)

    got = U.upstream_map(conn, window="30m", now=NOW)
    assert got["calls"] == 10
    assert got["retries"] == 4
    assert got["failed"] == 2
    assert got["retries_per_100"] == 40.0
    assert got["exhausted_rate"] == 0.2


def test_top_reason_share_is_of_retry_and_exhausted_events_not_calls(conn):
    for i in range(10):
        put(conn, "upstream.call", minutes_ago=1, i=i)
    for i in range(4):
        put(conn, "upstream.retry", minutes_ago=1, reason="HTTP 429", i=i)
    put(conn, "upstream.retry", minutes_ago=1, reason="ConnectError", i=0)
    put(conn, "upstream.failed", minutes_ago=1, reason="HTTP 429", i=0)

    got = U.upstream_map(conn, window="30m", now=NOW)
    assert got["top_reason"] == "HTTP 429"
    # 4 retries + 1 exhausted with 429, vs 1 ConnectError retry = 5/6.
    assert got["top_reason_share"] == round(5 / 6, 4)


def test_a_quiet_five_minutes_falls_back_to_the_chart_window(conn):
    for i in range(5):
        put(conn, "upstream.call", minutes_ago=20, i=i)
    put(conn, "upstream.retry", minutes_ago=20, reason="HTTP 503", i=0)

    got = U.upstream_map(conn, window="30m", now=NOW)
    assert got["calls"] == 5
    assert got["retries_per_100"] == 20.0
    assert got["top_reason"] == "HTTP 503"
    assert got["top_reason_share"] == 1.0


def test_series_null_rates_when_a_minute_had_no_calls(conn):
    put(conn, "upstream.call", minutes_ago=1, i=0)
    got = U.upstream_map(conn, window="30m", now=NOW)
    empty = [row for row in got["series"] if row["calls"] == 0]
    busy = [row for row in got["series"] if row["calls"]]
    assert empty and all(row["retries_per_100"] is None for row in empty)
    assert busy and busy[0]["retries_per_100"] == 0.0


def test_retrying_transport_writes_call_retry_and_failed_spans(settings, store, conn):
    configure_logging("INFO", None, console=False, force=True)
    store.install()

    class Script:
        def __init__(self, *outcomes):
            self.outcomes = list(outcomes)

        def __call__(self, request: httpx.Request) -> httpx.Response:
            outcome = self.outcomes.pop(0)
            return httpx.Response(outcome, json={"ok": False})

    client = httpx.Client(
        transport=H.RetryingTransport(
            httpx.MockTransport(Script(503, 503)),
            retries=1,
            sleep=lambda s: None,
        )
    )
    with pytest.raises(H.HttpFailure):
        client.get("https://api.example/")
    assert store.flush()

    names = [row["name"] for row in conn.execute("SELECT name FROM spans ORDER BY rowid")]
    assert names.count("upstream.call") == 1
    assert names.count("upstream.retry") == 1
    assert names.count("upstream.failed") == 1
    retry = conn.execute(
        "SELECT attrs FROM spans WHERE name = 'upstream.retry'"
    ).fetchone()
    assert json.loads(retry["attrs"])["reason"] == "HTTP 503"
