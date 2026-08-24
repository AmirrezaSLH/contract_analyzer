"""The metric store: the numbers, and the arithmetic that is easy to get wrong.

Offline throughout -- no model, no network, no key. Every claim is proved from
hand-built rows and a hand-computed answer, because the failures this module
can have are silent ones: a percentile that is right at n=5 and wrong at n=1,
a chart that closes its own gaps, a rate that reads 0% when it means "nothing
measured".

What is pinned here:

* **p50 / p95 match hand-computed nearest-rank values**, including n=1 and
  n=2, where percentile arithmetic usually goes wrong.
* **Three outcomes, not two**: the failure rate is `failed` + `interrupted`
  and never absorbs done-but-`needs_review`.
* **Empty is not zero.** A window with no runs returns `null` rates and zero
  counts, and the endpoint answers `200` -- `503` is reserved for a store that
  could not be built.
* **Buckets are epoch-aligned and gapless**, and a run lands in the bucket its
  `created_at` falls in whoever asks.
* **The evaluator slot names what it is showing**, because its own columns are
  NULL until the evaluator lands.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.metrics import MetricsStore, queries, windows

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
def conn(settings):
    conn = get_db(settings)
    conn.execute(
        "INSERT INTO documents (id, path, filename, content_hash) "
        "VALUES (1, 'data/raw/c.pdf', 'c.pdf', 'h')"
    )
    conn.commit()
    yield conn
    conn.close()


def record(
    conn,
    analysis_id: str,
    *,
    minutes_ago: int = 10,
    status: str = "done",
    latency_s: float | None = 60.0,
    cost_usd: float | None = 0.96,
    criteria: int = 5,
    needs_review: int = 0,
    capped: int = 0,
    mean_confidence: float | None = 0.8,
    quotes_total: int = 10,
    quotes_verified: int = 10,
    surface: str = "ui",
    states: dict[str, int] | None = None,
) -> None:
    """One finished run, written straight to the row `finish_analysis` writes.

    Straight to SQL rather than through a run: this file is about the queries,
    and a scripted five-criterion analysis to prove a percentile would be
    proving the runner instead. `tests/test_analyses.py` covers the writes.
    """
    created = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    report = None
    if status in ("done", "cancelled"):
        mix = states or {"Fully Compliant": criteria}
        report = json.dumps(
            {"results": [{"compliance_state": s} for s, n in mix.items() for _ in range(n)]}
        )
    conn.execute(
        "INSERT INTO analyses (analysis_id, trace_id, document_id, filename, surface, status,"
        " criteria_requested, criteria_completed, created_at, completed_at, report_json,"
        " latency_s, cost_usd, input_tokens, output_tokens, tool_calls, needs_review, capped,"
        " mean_confidence, quotes_total, quotes_verified)"
        " VALUES (?, ?, 1, 'c.pdf', ?, ?, 5, ?, ?, ?, ?, ?, ?, 1000, 200, 4, ?, ?, ?, ?, ?)",
        (
            analysis_id, "t" * 32, surface, status, criteria, created, created, report,
            latency_s, cost_usd, needs_review, capped, mean_confidence,
            quotes_total, quotes_verified,
        ),
    )
    conn.commit()


def summary(conn, window="24h"):
    return queries.summary(conn, window=window, now=NOW)


def timeseries(conn, window="24h", bucket="1h"):
    return queries.timeseries(conn, window=window, bucket=bucket, now=NOW)


# --------------------------------------------------------------------------
# Percentiles: the arithmetic, at the sizes that break it
# --------------------------------------------------------------------------


def test_one_run_is_its_own_p50_and_p95(conn):
    """n=1: both percentiles are the single value, and neither is a mean of it
    with nothing."""
    record(conn, "a1", latency_s=42.0, cost_usd=1.5)

    got = summary(conn)
    assert got["latency_s"]["p50"] == 42.0
    assert got["latency_s"]["p95"] == 42.0
    assert got["cost_usd"]["p50"] == 1.5
    assert got["cost_usd"]["p95"] == 1.5


def test_two_runs_split_p50_low_and_p95_high(conn):
    """n=2 is the edge: nearest rank puts p50 on the lower value -- rank
    ceil(2 x 0.5) = 1 -- and p95 on the upper, rank ceil(2 x 0.95) = 2. An
    implementation that interpolated would answer 75.0 for both."""
    record(conn, "a1", latency_s=50.0)
    record(conn, "a2", latency_s=100.0)

    got = summary(conn)["latency_s"]
    assert got["p50"] == 50.0
    assert got["p95"] == 100.0
    assert got["mean"] == 75.0


def test_percentiles_over_ten_runs_are_the_nearest_rank(conn):
    """1..10 seconds: p50 is rank ceil(10 x 0.5) = 5 and p95 is rank 10."""
    for index in range(1, 11):
        record(conn, f"a{index}", latency_s=float(index), cost_usd=index / 100)

    got = summary(conn)
    assert got["latency_s"] == {"p50": 5.0, "p95": 10.0, "mean": 5.5}
    assert got["cost_usd"]["p50"] == 0.05
    assert got["cost_usd"]["p95"] == 0.1


def test_a_run_without_a_latency_is_not_a_zero_in_the_percentile(conn):
    """A failed run never reached `finish_analysis`, so its `latency_s` is
    NULL. Counting it as 0.0 would drag p50 down and make the tile look
    better the more often the service broke."""
    record(conn, "a1", latency_s=60.0)
    record(conn, "a2", status="failed", latency_s=None, cost_usd=None)

    assert summary(conn)["latency_s"]["p50"] == 60.0


# --------------------------------------------------------------------------
# Three outcomes, not two
# --------------------------------------------------------------------------


def test_failure_rate_is_failed_plus_interrupted_and_excludes_needs_review(conn):
    """The distinction `01_findings.md` insists on: a run that finished and
    flagged a result for a human is a *quality* signal, not a reliability one,
    and folding it in would double-count it against the wrong threshold."""
    record(conn, "a1")
    record(conn, "a2", needs_review=3)
    record(conn, "a3", status="failed", latency_s=None, cost_usd=None)
    record(conn, "a4", status="interrupted", latency_s=None, cost_usd=None)

    got = summary(conn)
    assert got["runs"]["total"] == 4
    assert got["reliability"] == {"failure_rate": 0.5, "failed": 1, "interrupted": 1}
    # The needs-review run is not in it, and is counted where it belongs.
    assert got["quality"]["runs_needing_review"] == 1
    assert got["quality"]["needs_review"] == 3


def test_a_queued_run_is_not_in_the_failure_denominator(conn):
    """Dividing by every row would make the failure rate fall every time
    somebody submitted work."""
    record(conn, "a1", status="failed", latency_s=None, cost_usd=None)
    record(conn, "a2", status="queued", latency_s=None, cost_usd=None, criteria=0)

    got = summary(conn)
    assert got["runs"]["live"] == 1
    assert got["runs"]["settled"] == 1
    assert got["reliability"]["failure_rate"] == 1.0


# --------------------------------------------------------------------------
# Rates, denominators, and the empty window
# --------------------------------------------------------------------------


def test_quote_verification_and_needs_review_carry_their_denominators(conn):
    record(conn, "a1", quotes_total=10, quotes_verified=9, needs_review=1, capped=2)

    quality = summary(conn)["quality"]
    assert quality["quote_verification_rate"] == 0.9
    assert (quality["quotes_verified"], quality["quotes_total"]) == (9, 10)
    # Per criterion result, not per run: five criteria ran and one was flagged.
    assert quality["needs_review_rate"] == 0.2
    assert quality["cap_rate"] == 0.4


def test_an_empty_window_is_zeroes_and_nulls_not_a_failure(conn):
    """No quotes at all and no quotes verified mean opposite things, so a rate
    with nothing behind it is `null` rather than 0.0."""
    got = summary(conn)

    assert got["runs"]["total"] == 0
    assert got["reliability"]["failure_rate"] is None
    assert got["quality"]["quote_verification_rate"] is None
    assert got["quality"]["mean_confidence"] is None
    assert got["latency_s"] == {"p50": None, "p95": None, "mean": None}
    assert got["cost_usd"]["total"] == 0.0
    assert got["documents"] == 1


def test_the_window_excludes_what_is_older_than_it(conn):
    record(conn, "inside", minutes_ago=60, cost_usd=1.0)
    record(conn, "outside", minutes_ago=60 * 25, cost_usd=99.0)

    assert summary(conn, "24h")["cost_usd"]["total"] == 1.0
    assert summary(conn, "7d")["cost_usd"]["total"] == 100.0


def test_the_surface_split_is_free_and_reports_both_halves(conn):
    record(conn, "a1", surface="ui", cost_usd=1.0)
    record(conn, "a2", surface="cli", cost_usd=2.0)
    record(conn, "a3", surface="cli", cost_usd=3.0)

    by_surface = {row["surface"]: row for row in summary(conn)["surfaces"]}
    assert by_surface["cli"]["runs"] == 2
    assert by_surface["cli"]["cost_usd"] == 5.0
    assert by_surface["ui"]["runs"] == 1


# --------------------------------------------------------------------------
# The evaluator slot
# --------------------------------------------------------------------------


def test_the_evaluator_slot_says_it_is_showing_a_cap_rate(conn):
    """`analyses.evaluator_*` are NULL until the evaluator lands. A tile that
    labelled a cap rate as an accept rate would be the one dishonest number on
    the page."""
    record(conn, "a1", capped=1)

    evaluator = summary(conn)["quality"]["evaluator"]
    assert evaluator["available"] is False
    assert evaluator["accept_rate"] is None
    assert evaluator["showing"] == "cap_rate"
    assert evaluator["value"] == summary(conn)["quality"]["cap_rate"] == 0.2


# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------


def test_two_known_runs_land_in_the_buckets_their_timestamps_name(conn):
    record(conn, "a1", minutes_ago=15, cost_usd=1.0)  # 12:15 -> 12:00 bucket
    record(conn, "a2", minutes_ago=95, cost_usd=2.0)  # 10:55 -> 10:00 bucket

    series = {entry["bucket"]: entry for entry in timeseries(conn)}
    assert series["2026-08-24T12:00:00Z"]["runs"] == 1
    assert series["2026-08-24T12:00:00Z"]["cost_usd"] == 1.0
    assert series["2026-08-24T10:00:00Z"]["cost_usd"] == 2.0
    assert series["2026-08-24T11:00:00Z"]["runs"] == 0


def test_empty_buckets_are_returned_rather_than_closed_up(conn):
    """A chart that only receives the hours something happened in draws a busy
    night out of a quiet one."""
    record(conn, "a1", minutes_ago=15)

    series = timeseries(conn)
    assert len(series) == 25  # 24 whole hours plus the partial current one
    assert [entry["bucket"] for entry in series] == sorted(entry["bucket"] for entry in series)
    assert sum(entry["runs"] for entry in series) == 1
    quiet = next(entry for entry in series if entry["runs"] == 0)
    assert quiet["cost_usd"] == 0.0
    assert quiet["latency_s"] == {"p50": None, "p95": None}
    assert quiet["states"] == {}


def test_buckets_are_aligned_on_the_epoch_not_on_the_request(conn):
    """`bucket_expression` floors on unixepoch, so the same run is in the same
    bar whenever the page is refreshed."""
    record(conn, "a1", minutes_ago=15)

    at_half_past = timeseries(conn)
    later = queries.timeseries(
        conn, window="24h", bucket="1h", now=NOW + timedelta(minutes=20)
    )
    mine = [e for e in at_half_past if e["runs"]][0]["bucket"]
    assert [e for e in later if e["runs"]][0]["bucket"] == mine == "2026-08-24T12:00:00Z"


def test_a_bucket_carries_its_own_percentiles_and_state_mix(conn):
    record(conn, "a1", minutes_ago=15, latency_s=10.0,
           states={"Fully Compliant": 3, "Partially Compliant": 2})
    record(conn, "a2", minutes_ago=20, latency_s=30.0, states={"Non-Compliant": 5})

    entry = next(e for e in timeseries(conn) if e["runs"])
    assert entry["latency_s"] == {"p50": 10.0, "p95": 30.0}
    assert entry["states"] == {
        "Fully Compliant": 3, "Partially Compliant": 2, "Non-Compliant": 5,
    }


def test_the_window_picks_its_bucket_when_the_caller_does_not(conn):
    """24h -> 1h, 7d -> 6h, 30d -> 1d: the pairing the UI selector drives, in
    the API rather than in the browser."""
    assert windows.bucket_for("7d") == "6h"
    assert len(queries.timeseries(conn, window="7d", now=NOW)) == 29
    assert len(queries.timeseries(conn, window="30d", now=NOW)) == 31


def test_a_window_that_is_not_a_window_is_refused(conn):
    with pytest.raises(ValueError, match="24h"):
        summary(conn, "last tuesday")


# --------------------------------------------------------------------------
# The runs table
# --------------------------------------------------------------------------


def test_runs_is_global_newest_first_and_carries_the_trace_id(conn):
    """`GET /analyses` is per document on purpose; this is the list it does not
    serve. The trace id is the reason the table exists -- it is what turns a
    number on the page into a grep in app.jsonl."""
    record(conn, "old", minutes_ago=120)
    record(conn, "new", minutes_ago=5)

    rows = queries.runs(conn, limit=10)
    assert [row["analysis_id"] for row in rows] == ["new", "old"]
    assert rows[0]["trace_id"] == "t" * 32
    assert rows[0]["filename"] == "c.pdf"
    # Thirty kilobytes of report per row is not what a table wants.
    assert "report_json" not in rows[0]


def test_runs_honours_its_limit(conn):
    for index in range(5):
        record(conn, f"a{index}", minutes_ago=index)

    assert len(queries.runs(conn, limit=2)) == 2


# --------------------------------------------------------------------------
# The store is the same thing behind an object
# --------------------------------------------------------------------------


def test_the_store_reads_on_the_callers_connection(conn, settings):
    """It owns no connection of its own in this phase: a request already has
    one, and a metrics read is not a reason to open a second."""
    record(conn, "a1")
    store = MetricsStore(settings)

    assert store.summary(conn, window="24h")["runs"]["total"] == 1
    assert len(store.timeseries(conn, window="24h", bucket="1h")) == 25
    assert [row["analysis_id"] for row in store.runs(conn)] == ["a1"]


# --------------------------------------------------------------------------
# Through the API, over a run the scripted model actually produced
# --------------------------------------------------------------------------


@pytest.fixture
def analysed(tmp_path, monkeypatch):
    """One real five-criterion run against the scripted model, and the app that
    can be asked about it. The numbers on the page are then checked against the
    report rather than against a row this file wrote."""
    import struct

    from conftest import make_chunk, scripted_client
    from contract_analyzer.embeddings.fake import FakeEmbedder
    from contract_analyzer.generation import tools as T
    from contract_analyzer.report import analyze_document
    from contract_analyzer.retrieval.base import RetrievalResult
    from test_report import CLAUSE, script

    settings = Settings(
        anthropic_api_key="k",
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=4,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path,
        assets_dir=tmp_path / "assets",
        log_file=None,
        analysis_workers=1,
        analysis_max_tool_calls=4,
        structure_fix_rounds=0,
    )

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=[make_chunk(1, CLAUSE)], candidates=20, top_k=top_k or 6)

    monkeypatch.setattr(T, "retrieve", retrieve)

    conn = get_db(settings)
    conn.execute(
        "INSERT INTO documents (id, path, filename, content_hash, page_count) "
        "VALUES (1, 'data/raw/c.pdf', 'c.pdf', 'h', 21)"
    )
    cursor = conn.execute(
        "INSERT INTO chunks (document_id, ordinal, content, page, page_label, section, "
        "section_path, embedding_model) VALUES (1, 0, ?, 8, '9', '6.6', '[]', 'fake-hash')",
        (CLAUSE,),
    )
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, document_id, embedding) VALUES (?, 1, ?)",
        (cursor.lastrowid, struct.pack("4f", *([0.5] * 4))),
    )
    conn.commit()

    report = analyze_document(1, conn, object(), settings, scripted_client(script()))
    conn.close()

    from fastapi.testclient import TestClient

    from contract_analyzer.api.main import create_app

    app = create_app(settings, embedder=FakeEmbedder(settings), client=None,
                     static_dir=tmp_path / "no-such-bundle")
    with TestClient(app) as client:
        yield client, report


def test_the_summary_matches_the_report_the_run_produced(analysed):
    """The acceptance check from `Metric_Store.md` §9: cost, latency, quote
    verification and mean confidence on the page are the ones in the report,
    not numbers the query layer arrived at on its own."""
    client, report = analysed

    got = client.get("/api/metrics/summary?window=24h").json()
    quotes = [quote for result in report.results for quote in result.relevant_quotes]

    assert got["runs"]["total"] == 1
    assert got["runs"]["criteria"] == len(report.results)
    assert got["cost_usd"]["total"] == pytest.approx(report.totals.cost_usd)
    assert got["cost_usd"]["p50"] == pytest.approx(report.totals.cost_usd)
    assert got["latency_s"]["p50"] == pytest.approx(report.totals.latency_s)
    assert got["quality"]["mean_confidence"] == pytest.approx(report.totals.mean_confidence)
    assert got["quality"]["quotes_total"] == len(quotes) > 0
    assert got["quality"]["quote_verification_rate"] == pytest.approx(
        sum(1 for quote in quotes if quote.verified) / len(quotes)
    )
    assert got["reliability"]["failure_rate"] == 0.0


def test_the_live_tile_comes_from_the_runner_and_not_from_a_table(analysed):
    """Active and queued are facts about this process. A table would be
    describing the last one."""
    client, _ = analysed

    assert client.get("/api/metrics/summary").json()["live"] == {
        "running": 0, "queued": 0, "active": 0,
    }


def test_the_runs_table_lists_the_run_with_the_trace_it_ran_under(analysed):
    client, report = analysed

    rows = client.get("/api/metrics/runs").json()
    assert [row["analysis_id"] for row in rows] == [report.analysis_id]
    assert rows[0]["trace_id"] == report.trace_id
    assert rows[0]["surface"] == "cli"


def test_a_malformed_window_is_a_422_in_this_apis_envelope(analysed):
    client, _ = analysed

    response = client.get("/api/metrics/summary?window=last-tuesday")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"
