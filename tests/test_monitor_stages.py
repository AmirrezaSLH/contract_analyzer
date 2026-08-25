"""The Monitor tab's Stages band: where a span failed, not whether a run did."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.metrics import MetricsStore, stages, windows

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
    status: str = "ok",
    latency_ms: float = 100.0,
    i: int = 0,
) -> None:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")
    conn.execute(
        "INSERT INTO spans (span_id, name, status, latency_ms, ts) VALUES (?, ?, ?, ?, ?)",
        (f"{name}-{minutes_ago}-{i}", name, status, latency_ms, ts),
    )
    conn.commit()


def test_windows_accept_five_minutes():
    assert windows.seconds("5m") == 300
    assert windows.seconds("30s") == 30
    assert windows.bucket_for("30m") == "1m"
    assert windows.bucket_for("1h") == "1m"


def test_empty_spans_are_nulls_not_a_failure(conn, store: MetricsStore):
    got = store.stages(conn, window="24h")
    assert got["name"] is None
    assert got["n"] == 0
    assert got["error_rate"] is None
    assert got["errors_total"] is None
    assert got["min_samples"] == stages.MIN_SAMPLES
    assert got["series"]
    assert all(
        row["n"] == 0 and row["error_rate"] is None and row["errors_total"] is None
        for row in got["series"]
    )


def test_one_of_one_is_not_the_paged_worst(conn, store: MetricsStore):
    """A single failed retrieve is 100%, and that is not a rate we page on.
    It is still the name on the tile, with n=1 so the chip can say not enough."""
    put(conn, "retrieve", minutes_ago=1, status="error", latency_ms=50, i=0)
    for i in range(12):
        put(conn, "agent.call", minutes_ago=1, status="ok", latency_ms=200, i=i)

    got = stages.stage_map(conn, window="24h", now=NOW)
    # agent.call has n>=10 and 0% errors; retrieve is 1-of-1. Worst among
    # enough samples is agent.call, not the singleton failure.
    assert got["name"] == "agent.call"
    assert got["n"] == 12
    assert got["error_rate"] == 0.0


def test_among_enough_samples_the_higher_error_rate_wins(conn, store: MetricsStore):
    for i in range(10):
        status = "error" if i < 4 else "ok"
        put(conn, "ingest.parse", minutes_ago=1, status=status, latency_ms=80, i=i)
    for i in range(10):
        put(conn, "chat", minutes_ago=1, status="ok", latency_ms=40, i=i)

    got = stages.stage_map(conn, window="24h", now=NOW)
    assert got["name"] == "ingest.parse"
    assert got["n"] == 10
    assert got["errors"] == 4
    assert got["error_rate"] == 0.4
    assert got["errors_total"] == 4


def test_a_quiet_five_minutes_falls_back_to_the_chart_window(conn, store: MetricsStore):
    for i in range(10):
        status = "error" if i == 0 else "ok"
        put(conn, "ingest.embed", minutes_ago=60, status=status, latency_ms=1000, i=i)

    got = stages.stage_map(conn, window="24h", now=NOW)
    assert got["name"] == "ingest.embed"
    assert got["n"] == 10
    assert got["error_rate"] == 0.1


def test_series_includes_empty_buckets_and_null_rates(conn, store: MetricsStore):
    for i in range(10):
        put(conn, "retrieve", minutes_ago=1, status="ok", latency_ms=10, i=i)

    got = stages.stage_map(conn, window="24h", now=NOW)
    assert got["name"] == "retrieve"
    filled = [row for row in got["series"] if row["n"]]
    assert len(filled) == 1
    assert filled[0]["error_rate"] == 0.0
    assert filled[0]["errors_total"] == 0
    empty = next(row for row in got["series"] if row["n"] == 0)
    assert empty["error_rate"] is None
    assert empty["errors_total"] is None


def test_errors_total_sums_every_named_stage(conn):
    for i in range(10):
        put(conn, "ingest.parse", minutes_ago=1, status="error" if i < 4 else "ok", i=i)
    for i in range(10):
        put(conn, "retrieve", minutes_ago=1, status="error" if i < 2 else "ok", i=i)

    got = stages.stage_map(conn, window="24h", now=NOW)
    assert got["name"] == "ingest.parse"
    assert got["errors"] == 4
    assert got["errors_total"] == 6
    filled = next(row for row in got["series"] if row["n"])
    assert filled["errors_total"] == 6


def test_names_outside_the_short_list_are_ignored(conn, store: MetricsStore):
    for i in range(20):
        put(conn, "analysis.document", minutes_ago=1, status="error", i=i)
    got = stages.stage_map(conn, window="24h", now=NOW)
    assert got["name"] is None
