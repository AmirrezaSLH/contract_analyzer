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
import time
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
def store(settings) -> MetricsStore:
    """Building the store applies `metrics.sql`, which is what creates `spans`.

    Not installed: `install()` is what attaches the handler and starts the
    writer thread, and most of this file is about the queries.
    """
    store = MetricsStore(settings)
    yield store
    store.close()


@pytest.fixture
def conn(settings, store):
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

    from contract_analyzer.logger import configure_logging
    from contract_analyzer.metrics import MetricsStore

    configure_logging("INFO", None, console=False, force=True)

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

    # The wiring `scripts/analyze.py` does, in the order it does it: a store
    # built from settings, installed, and then the same `analyze_document` the
    # API's worker calls. No API is involved in producing these spans.
    cli_store = MetricsStore(settings).install()
    report = analyze_document(1, conn, object(), settings, scripted_client(script()))
    assert cli_store.flush()
    cli_store.close()
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


# --------------------------------------------------------------------------
# The span handler: the discipline, proved rather than asserted in a docstring
# --------------------------------------------------------------------------


def log_span(logger, name: str, /, **attrs) -> None:
    """A `span.end` record as `logger.span()` writes one, without timing a
    block. Hand-built, so a test can send exactly the attributes it means to."""
    logger.info("span.end", extra={"span": name, "status": "ok", **attrs})


@pytest.fixture
def installed(settings, store):
    """The store with its handler on the root logger and its writer running."""
    from contract_analyzer.logger import configure_logging, get_logger

    configure_logging("INFO", None, console=False, force=True)
    store.install()
    yield store, get_logger("test.spans")
    store.close()


def rows(conn, **where):
    sql = "SELECT * FROM spans"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return [dict(row) for row in conn.execute(sql + " ORDER BY ts, rowid", tuple(where.values()))]


def test_one_row_per_span_end_and_none_per_span_start(installed, conn):
    """`span.start` carries nothing `span.end` does not, including the latency.
    A row for each would double the table to record nothing."""
    store, logger = installed

    logger.info("span.start", extra={"span": "retrieve"})
    log_span(logger, "retrieve", latency_ms=12.5, mode="hybrid", document_id=1)
    logger.info("something.else", extra={"span": "retrieve"})
    assert store.flush()

    stored = rows(conn)
    assert len(stored) == 1
    assert stored[0]["name"] == "retrieve"
    assert stored[0]["latency_ms"] == 12.5
    assert stored[0]["document_id"] == 1
    assert json.loads(stored[0]["attrs"]) == {"mode": "hybrid"}
    assert store.dropped == 0


def test_the_promoted_columns_come_out_of_the_bag_and_the_rest_stay_json(installed, conn):
    """Every KPI query touches surface, criterion, document_id, model, tokens
    and cost; json_extract over a million rows to group by model is a table
    scan with extra steps. Everything else is one json_extract away instead of
    one migration away."""
    store, logger = installed

    log_span(logger, "agent.call", surface="analysis", model="claude-sonnet-5",
             input_tokens=1000, output_tokens=200, cost_usd=0.004,
             latency_ms=900.0, turn=2, effort="medium", stop_reason="end_turn")
    assert store.flush()

    row = rows(conn)[0]
    assert row["model"] == "claude-sonnet-5"
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert row["cost_usd"] == 0.004
    assert row["surface"] == "analysis"
    assert json.loads(row["attrs"]) == {"turn": 2, "effort": "medium",
                                        "stop_reason": "end_turn"}


def test_a_malformed_attribute_never_raises_and_never_loses_the_span(installed, conn):
    """This runs on a criterion thread inside a run that is already a minute
    long and a dollar deep. An exception out of the handler is the one outcome
    that is not acceptable."""
    store, logger = installed

    class Awkward:
        def __repr__(self):
            raise RuntimeError("not even repr")

    log_span(logger, "agent.call", document_id="not an int", cost_usd="free",
             input_tokens=None, weird=Awkward(), latency_ms="soon")
    assert store.flush()

    row = rows(conn)[0]
    assert row["name"] == "agent.call"
    # Unparseable values become NULL rather than taking the row with them.
    assert row["document_id"] is None
    assert row["cost_usd"] is None
    assert row["latency_ms"] is None
    # And a value even `str()` cannot handle costs that value, not the bag and
    # not the row: "every span is stored" is a claim, so it has to hold here.
    assert json.loads(row["attrs"])["weird"] == "<unserialisable>"
    assert store.dropped == 0


def test_a_full_queue_drops_and_counts_instead_of_blocking(settings, conn):
    """The handler must never block a criterion thread in order to record that
    a criterion thread was busy -- and it must say how much it lost, because a
    metrics system that silently loses data is worse than one that admits it."""
    from contract_analyzer.logger import LogRecord
    from contract_analyzer.metrics.handler import SpanHandler

    handler = SpanHandler(lambda: None, capacity=2)  # never started: nothing drains

    def record(name):
        made = LogRecord("test", 20, __file__, 1, "span.end", None, None)
        made.span = name
        made.status = "ok"
        return made

    for index in range(10):
        handler.emit(record(f"s{index}"))

    assert handler.dropped == 8
    assert handler.written == 0


def test_the_dropped_counter_is_on_the_summary(installed, conn):
    store, logger = installed
    log_span(logger, "chat", cost_usd=0.01, latency_ms=800.0)
    assert store.flush()

    got = store.summary(conn)["spans"]
    assert got == {"written": 1, "dropped": 0}


# --------------------------------------------------------------------------
# run_id: what belongs to a run, and what correctly does not
# --------------------------------------------------------------------------


def test_a_runs_spans_are_its_own_even_inside_one_trace(installed, conn):
    """One trace legitimately contains an upload *and* an analysis -- and two
    analyses can share a trace when one request starts both. `run_id` is what
    keeps the waterfall a run's rather than a trace's."""
    from contract_analyzer.logger import run_context, trace_context

    store, logger = installed

    with trace_context("t" * 32):
        log_span(logger, "ingest.file")  # no run: not part of one
        with run_context("run-a"):
            log_span(logger, "analysis.criterion", criterion="password_management")
        with run_context("run-b"):
            log_span(logger, "analysis.criterion", criterion="network_auth")
        log_span(logger, "chat", cost_usd=0.01)  # chat is not a run
    assert store.flush()

    assert [row["name"] for row in rows(conn, run_id="run-a")] == ["analysis.criterion"]
    assert [row["criterion"] for row in rows(conn, run_id="run-b")] == ["network_auth"]
    unattached = [row["name"] for row in rows(conn) if row["run_id"] is None]
    assert sorted(unattached) == ["chat", "ingest.file"]
    # All four share the trace, which is what makes the point.
    assert len(rows(conn, trace_id="t" * 32)) == 4


# --------------------------------------------------------------------------
# What spans make answerable that `analyses` cannot
# --------------------------------------------------------------------------


def test_chat_cost_is_a_span_query_and_never_a_run_row(installed, conn):
    """Chat is stateless by design: it writes no row anywhere. Giving it one in
    `analyses` would mean every analysis KPI needed `WHERE surface != 'chat'`
    forever, so it is `spans WHERE name = 'chat'` instead."""
    store, logger = installed

    log_span(logger, "chat", cost_usd=0.02, latency_ms=1000.0, document_id=1)
    log_span(logger, "chat", cost_usd=0.04, latency_ms=3000.0, document_id=1)
    assert store.flush()

    got = store.summary(conn)
    assert got["chat"]["turns"] == 2
    assert got["chat"]["cost_usd"] == 0.06
    assert got["chat"]["cost_per_turn"] == 0.03
    assert got["chat"]["latency_ms"] == {"p50": 1000.0, "p95": 3000.0}
    # And it is not in the analysis numbers.
    assert got["runs"]["total"] == 0


def test_cost_per_model_covers_analysis_and_chat_in_one_pass(installed, conn):
    """The reason this waited for spans instead of mining `report_json`: that
    would have been analysis-only, and `analyses` has no model column."""
    store, logger = installed

    log_span(logger, "agent.call", surface="analysis", model="claude-sonnet-5",
             cost_usd=0.004, input_tokens=1000, output_tokens=200)
    log_span(logger, "agent.call", surface="chat", model="claude-sonnet-5",
             cost_usd=0.001, input_tokens=300, output_tokens=50)
    log_span(logger, "agent.call", surface="chat", model="claude-haiku-4-5",
             cost_usd=0.0002, input_tokens=300, output_tokens=50)
    assert store.flush()

    by_model = {row["model"]: row for row in store.summary(conn)["cost_by_model"]}
    assert by_model["claude-sonnet-5"]["calls"] == 2
    assert by_model["claude-sonnet-5"]["cost_usd"] == 0.005
    assert by_model["claude-sonnet-5"]["input_tokens"] == 1300
    assert by_model["claude-haiku-4-5"]["calls"] == 1


def test_spans_of_a_deleted_documents_run_survive_the_delete(settings, conn, store):
    """The reason there is no foreign key on `spans`, and the same argument as
    `analyses`: history that vanishes when somebody tidies up the corpus is not
    history."""
    from contract_analyzer.documents import delete_document

    record(conn, "a1")
    conn.execute(
        "INSERT INTO spans (span_id, run_id, name, ts, document_id) "
        "VALUES ('s1', 'a1', 'analysis.document', ?, 1)",
        ((NOW - timedelta(minutes=10)).isoformat(timespec="milliseconds"),),
    )
    conn.commit()

    assert delete_document(conn, 1)

    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert len(rows(conn, run_id="a1")) == 1
    assert len(queries.runs(conn)) == 1


# --------------------------------------------------------------------------
# The waterfall
# --------------------------------------------------------------------------


def test_the_waterfall_is_a_tree_that_resolves(installed, conn):
    """The shape `Metric_Store.md` predicts, built from the real `span()`
    context manager so the parent links are the ones production writes."""
    from contract_analyzer.logger import run_context, span, trace_context

    store, logger = installed

    with (
        trace_context(),
        run_context("run-1"),
        span("analysis.document", logger, document_id=1),
        span("analysis.criterion", logger, criterion="password_management"),
        span("agent.run", logger, surface="analysis"),
    ):
        with span("agent.call", logger, model="claude-sonnet-5", cost_usd=0.004):
            pass
        with (
            span("agent.tool", logger, tool="search_contract"),
            span("retrieve", logger, mode="hybrid", document_id=1),
        ):
            pass
    assert store.flush()

    tree = store.spans(conn, "run-1")
    assert [node["name"] for node in tree] == ["analysis.document"]
    criterion = tree[0]["children"][0]
    assert criterion["name"] == "analysis.criterion"
    assert criterion["criterion"] == "password_management"
    run = criterion["children"][0]
    assert [child["name"] for child in run["children"]] == ["agent.call", "agent.tool"]
    assert run["children"][0]["model"] == "claude-sonnet-5"
    assert [leaf["name"] for leaf in run["children"][1]["children"]] == ["retrieve"]
    assert run["children"][1]["children"][0]["attrs"]["mode"] == "hybrid"


def test_a_run_with_no_spans_is_an_empty_tree_not_an_error(conn, store):
    """A run from before this table existed is still in `/metrics/runs` beside
    it, so a 404 here would be wrong about a run that plainly exists."""
    record(conn, "a1")

    assert store.spans(conn, "a1") == []


def test_prune_drops_old_spans_and_leaves_the_analyses_alone(conn, store):
    """The reports are the deliverable; a retention policy that took them with
    the telemetry would be deleting the wrong half."""
    record(conn, "a1")
    for index, minutes in enumerate((5, 60 * 24 * 40)):
        conn.execute(
            "INSERT INTO spans (span_id, run_id, name, ts) VALUES (?, 'a1', 'retrieve', ?)",
            (f"s{index}", (NOW - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")),
        )
    conn.commit()

    cut = (NOW - timedelta(days=30)).isoformat(timespec="milliseconds")
    assert store.prune(conn, cut) == 1
    assert len(rows(conn)) == 1
    assert len(queries.runs(conn)) == 1


def test_the_command_line_populates_spans_with_no_api_involved(analysed, settings):
    """`Metric_Store.md` §9: `make analyze` fills the same table the API does.
    A dashboard that only saw HTTP traffic would measure the surface, not the
    system -- and this run was produced before the app was built at all.

    The fixture does exactly what `scripts/analyze.py` does: build the store
    from settings, install it, call `analyze_document`.
    """
    client, report = analysed

    tree = client.get(f"/api/metrics/runs/{report.analysis_id}/spans").json()
    assert [node["name"] for node in tree] == ["analysis.document"]
    criteria = [child for child in tree[0]["children"]
                if child["name"] == "analysis.criterion"]
    assert len(criteria) == len(report.results)
    assert {child["criterion"] for child in criteria} == {
        result.criterion_id for result in report.results
    }


def test_the_summary_costs_the_run_by_model_from_its_agent_calls(analysed):
    """`analyses` has no model column; this number exists only because the
    calls were spans."""
    client, report = analysed

    got = client.get("/api/metrics/summary").json()
    assert len(got["cost_by_model"]) == 1
    model = got["cost_by_model"][0]
    assert model["model"] == "claude-sonnet-5"
    assert model["cost_usd"] == pytest.approx(report.totals.cost_usd, abs=1e-6)
    assert model["input_tokens"] == report.totals.input_tokens
    assert got["spans"]["dropped"] == 0


def test_a_chat_turn_lands_in_spans_and_in_the_per_model_cost(settings, monkeypatch):
    """`Metric_Store.md` §9: chat writes no row anywhere, so its cost exists
    only because the turn was a span. It must also appear in cost-per-model,
    which is the property that made `spans` worth building instead of mining
    `report_json` -- one query covering analysis and chat together."""
    from fastapi.testclient import TestClient

    from conftest import ScriptedAPI, make_chunk, scripted_client
    from contract_analyzer.api.main import create_app
    from contract_analyzer.embeddings.fake import FakeEmbedder
    from contract_analyzer.generation import tools as T
    from contract_analyzer.retrieval.base import RetrievalResult
    from test_api import CLAUSE, chat_turns

    settings = Settings(
        anthropic_api_key="k",
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=4,
        db_path=settings.db_path,
        raw_dir=settings.raw_dir,
        assets_dir=settings.assets_dir,
        log_file=None,
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
    conn.commit()
    conn.close()

    api = ScriptedAPI(*chat_turns())
    app = create_app(settings, embedder=FakeEmbedder(settings), client=scripted_client(api),
                     static_dir=settings.raw_dir / "no-such-bundle")
    with TestClient(app) as client:
        answer = client.post(
            "/api/chat", json={"document_id": 1, "question": "passwords?", "stream": False}
        ).json()
        assert app.state.metrics.flush()
        got = client.get("/api/metrics/summary").json()

    assert got["chat"]["turns"] == 1
    assert got["chat"]["cost_usd"] == pytest.approx(answer["cost_usd"])
    assert got["chat"]["latency_ms"]["p50"] is not None
    # The same dollars, reached the other way: through the agent.call spans the
    # turn made. `analyses` has no model column and could answer neither.
    by_model = {row["model"]: row for row in got["cost_by_model"]}
    assert by_model[answer["model"]]["cost_usd"] == pytest.approx(answer["cost_usd"])
    # And chat is not an analysis: no run row was invented for it.
    assert got["runs"]["total"] == 0


def test_an_api_run_puts_the_job_span_at_the_root_of_the_waterfall(settings, monkeypatch):
    """`api.analysis` covers queueing as well as running, so it is the root the
    UI wants: without it the wait for a worker would be missing from the one
    view that should show it."""
    import struct

    from fastapi.testclient import TestClient

    from conftest import ScriptedAPI, make_chunk, scripted_client
    from contract_analyzer.api.main import create_app
    from contract_analyzer.embeddings.fake import FakeEmbedder
    from contract_analyzer.generation import tools as T
    from contract_analyzer.retrieval.base import RetrievalResult
    from test_report import CLAUSE, CRITERIA, turns_for

    # A fresh Settings rather than `model_copy`: that skips validation, and
    # `anthropic_api_key` would stay a plain str where a SecretStr is expected.
    settings = Settings(
        anthropic_api_key="k",
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=4,
        db_path=settings.db_path,
        raw_dir=settings.raw_dir,
        assets_dir=settings.assets_dir,
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
    conn.close()

    one = CRITERIA[0]
    api = ScriptedAPI(*turns_for(one))
    app = create_app(settings, embedder=FakeEmbedder(settings), client=scripted_client(api),
                     static_dir=settings.raw_dir / "no-such-bundle")
    with TestClient(app) as client:
        submitted = client.post(
            "/api/analyses", json={"document_id": 1, "criteria": [one.id]}
        ).json()
        analysis_id = submitted["analysis_id"]
        for _ in range(200):
            if client.get(f"/api/analyses/{analysis_id}").json()["status"] == "done":
                break
            time.sleep(0.02)
        assert app.state.metrics.flush()

        tree = client.get(f"/api/metrics/runs/{analysis_id}/spans").json()

    assert [node["name"] for node in tree] == ["api.analysis"]
    assert [child["name"] for child in tree[0]["children"]] == ["analysis.document"]
    names = {child["name"] for child in tree[0]["children"][0]["children"]}
    assert names == {"analysis.criterion"}


# --------------------------------------------------------------------------
# criterion_results: the grain between a run and a span
# --------------------------------------------------------------------------


def test_a_finished_run_writes_one_row_per_criterion(analysed):
    """Written by `finish_analysis` from the report it is already holding, so
    there is no second pass and no second source of truth."""
    client, report = analysed

    conn = get_db(client.app.state.settings)
    try:
        rows = {
            row["criterion_id"]: dict(row)
            for row in conn.execute("SELECT * FROM criterion_results WHERE run_id = ?",
                                    (report.analysis_id,))
        }
    finally:
        conn.close()

    assert set(rows) == {result.criterion_id for result in report.results}
    first = report.results[0]
    stored = rows[first.criterion_id]
    assert stored["state"] == first.compliance_state
    assert stored["confidence"] == first.confidence
    assert stored["raw_confidence"] == first.raw_confidence
    assert stored["ended_by"] == first.ended_by
    assert stored["tool_calls"] == first.tool_calls
    assert stored["quotes_total"] == len(first.relevant_quotes)
    assert stored["quotes_verified"] == sum(1 for q in first.relevant_quotes if q.verified)
    # Declared, and honestly empty until the evaluator lands.
    assert stored["evaluator_verdict"] is None


def test_a_run_records_itself_even_with_no_metrics_tables(settings, tmp_path):
    """`analyses.py` must not import `metrics`: storage does not depend on
    telemetry. So a process that never built a store still writes the run --
    it just has no per-criterion history to write it into."""
    from contract_analyzer.analyses import finish_analysis, get_analysis, queue_analysis
    from contract_analyzer.report import AnalysisReport

    conn = get_db(settings)  # no MetricsStore anywhere: `criterion_results` is absent
    try:
        assert "criterion_results" not in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master")
        }
        queue_analysis(conn, "a1", 1, criteria=["c1"])
        report = AnalysisReport(analysis_id="a1", document_id=1,
                                created_at="2026-08-24T00:00:00+00:00")

        assert finish_analysis(conn, "a1", report) is True
        assert get_analysis(conn, "a1").status == "done"
    finally:
        conn.close()


def test_criterion_mix_is_the_drift_and_calibration_query(conn, store):
    """The same criterion coming back with a different state, and the gap
    between the model's own estimate and the derived confidence."""
    record(conn, "a1")
    record(conn, "a2")
    for run, state, confidence, raw in (
        ("a1", "Fully Compliant", 0.9, 0.95),
        ("a2", "Partially Compliant", 0.5, 0.9),
    ):
        conn.execute(
            "INSERT INTO criterion_results (run_id, criterion_id, state, confidence, "
            "raw_confidence, needs_review, ended_by) VALUES (?, 'password_management', "
            "?, ?, ?, 0, 'stop')",
            (run, state, confidence, raw),
        )
    conn.commit()

    mix = {row["state"]: row for row in store.criterion_mix(conn, window="24h")}
    assert set(mix) == {"Fully Compliant", "Partially Compliant"}
    assert mix["Partially Compliant"]["runs"] == 1
    # The calibration gap: the model said 0.9 and the derivation said 0.5.
    assert mix["Partially Compliant"]["raw_confidence"] == 0.9
    assert mix["Partially Compliant"]["confidence"] == 0.5


def test_the_table_backfills_from_reports_that_predate_it(analysed):
    """Why phase 3 could land last: `json_each` over `report_json` recovers
    every run, and rows already written are left exactly as they are."""
    client, report = analysed
    settings = client.app.state.settings

    conn = get_db(settings)
    try:
        conn.execute("DELETE FROM criterion_results")
        conn.commit()
        store = MetricsStore(settings)

        assert store.backfill_criteria(conn) == len(report.results)
        assert store.backfill_criteria(conn) == 0  # INSERT OR IGNORE: idempotent

        rows = {
            row["criterion_id"]: dict(row)
            for row in conn.execute("SELECT * FROM criterion_results")
        }
        first = report.results[0]
        assert rows[first.criterion_id]["state"] == first.compliance_state
        assert rows[first.criterion_id]["confidence"] == first.confidence
        assert rows[first.criterion_id]["quotes_verified"] == sum(
            1 for quote in first.relevant_quotes if quote.verified
        )
        store.close()
    finally:
        conn.close()
