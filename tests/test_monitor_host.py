"""The Monitor tab's Host band: this process's RAM, and the disk under the DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.metrics import MetricsStore
from contract_analyzer.metrics import host as host_mod
from contract_analyzer.metrics import windows
from contract_analyzer.metrics.sampler import snapshot

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
        monitor_sample_seconds=30,
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
    *,
    minutes_ago: float,
    rss_pct: float,
    disk_used_pct: float,
    rss_mb: float = 100.0,
    disk_used_gb: float = 1.0,
    disk_total_gb: float = 10.0,
    i: int = 0,
) -> None:
    ts = (NOW - timedelta(minutes=minutes_ago, seconds=i)).isoformat(
        timespec="milliseconds"
    )
    conn.execute(
        """INSERT INTO system_samples
           (ts, rss_mb, rss_pct, disk_used_pct, disk_used_gb, disk_total_gb)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ts, rss_mb, rss_pct, disk_used_pct, disk_used_gb, disk_total_gb),
    )
    conn.commit()


def test_snapshot_reads_this_process(settings):
    row = snapshot(db_path=settings.db_path)
    assert row["rss_mb"] is not None and row["rss_mb"] > 0
    assert row["rss_pct"] is not None and 0 < row["rss_pct"] < 1
    assert row["disk_used_pct"] is not None and 0 <= row["disk_used_pct"] <= 1
    assert row["disk_total_gb"] > 0


def test_empty_samples_are_nulls_not_a_failure(conn, store: MetricsStore):
    got = store.host(conn, window="30m")
    assert got["rss_pct"] is None
    assert got["disk_used_pct"] is None
    assert got["series"]
    assert all(row["rss_pct"] is None and row["disk_used_pct"] is None for row in got["series"])


def test_tiles_are_the_latest_sample_not_an_average(conn):
    put(conn, minutes_ago=60, rss_pct=0.5, disk_used_pct=0.4, rss_mb=200)
    put(conn, minutes_ago=1, rss_pct=0.18, disk_used_pct=0.41, rss_mb=360)
    got = host_mod.host_map(conn, window="1h", interval=30, now=NOW)
    assert got["rss_pct"] == 0.18
    assert got["rss_mb"] == 360
    assert got["disk_used_pct"] == 0.41


def test_a_bucket_keeps_its_last_sample(conn):
    put(conn, minutes_ago=1, rss_pct=0.1, disk_used_pct=0.2, i=2)
    put(conn, minutes_ago=1, rss_pct=0.3, disk_used_pct=0.5, i=1)
    got = host_mod.host_map(conn, window="30m", interval=30, now=NOW)
    filled = [row for row in got["series"] if row["rss_pct"] is not None]
    assert len(filled) == 1
    assert filled[0]["rss_pct"] == 0.3
    assert filled[0]["disk_used_pct"] == 0.5


def test_host_bars_match_the_sampler_interval(conn):
    got = host_mod.host_map(conn, window="30m", interval=30, now=NOW)
    assert got["bucket"] == "30s"
    assert len(got["series"]) == len(windows.bucket_starts("30m", "30s", now=NOW))


def test_a_day_of_host_charts_is_hourly_not_thousands_of_ticks(conn):
    got = host_mod.host_map(conn, window="24h", interval=30, now=NOW)
    assert got["bucket"] == "1h"
    assert len(got["series"]) == len(windows.bucket_starts("24h", "1h", now=NOW))


def test_a_week_and_a_month_follow_the_window_pairing(conn):
    week = host_mod.host_map(conn, window="7d", interval=30, now=NOW)
    month = host_mod.host_map(conn, window="30d", interval=30, now=NOW)
    assert week["bucket"] == "6h"
    assert month["bucket"] == "1d"
    assert len(week["series"]) == len(windows.bucket_starts("7d", "6h", now=NOW))
    assert len(month["series"]) == len(windows.bucket_starts("30d", "1d", now=NOW))


def test_tick_writes_a_row(conn, store: MetricsStore):
    store.sampler.tick()
    n = conn.execute("SELECT count(*) FROM system_samples").fetchone()[0]
    assert n == 1
    got = store.host(conn, window="30m")
    assert got["bucket"] == "30s"
    assert got["rss_pct"] is not None
    assert got["disk_used_pct"] is not None
