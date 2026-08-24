"""The document runner: five criteria in parallel, and what comes back.

`analyze_criterion` is tested next door. What this file is about is the layer
above it, and every claim here is a consequence of running five agents at once:

* the report's `results` are in **criteria order**, not finishing order, so two
  runs of the same contract diff line by line;
* each criterion gets **its own connection**, because `db.py` says sharing one
  across threads is a bug;
* the **trace id survives the pool**, which it does not without
  `copy_context()` -- that is the assertion protecting the demo's "same id in
  app.jsonl" moment;
* `on_event` is called **serially and tagged with the criterion**, so no
  consumer needs a lock and no event is orphaned;
* cancellation skips what has not started, and the partial report says which.

The model is the scripted SSE transport, so this is offline and keyless. The
pool runs at one worker for the scripted tests -- `ScriptedAPI` pops its
outcomes off a list with no lock, so five threads racing for them have no
deterministic order. Parallelism itself is tested separately, against a script
where every response is the same and order cannot matter.
"""

from __future__ import annotations

import json
import struct
import threading
import time

import pytest

from conftest import ScriptedAPI, critic_turn, make_chunk, scripted_client, sse_message
from contract_analyzer.compliance import get_criteria
from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.generation import tools as T
from contract_analyzer.logger import trace_context
from contract_analyzer.report import analyze_document, totals_of

DIM = 4
CLAUSE = "Supplier shall rotate credentials and encrypt data in transit at all times."
CRITERIA = get_criteria()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="k",
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=DIM,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path,
        assets_dir=tmp_path / "assets",
        log_file=None,
        analysis_workers=1,
        analysis_max_tool_calls=4,
        structure_fix_rounds=0,
    )


@pytest.fixture
def conn(settings):
    """One document, one chunk. The chunk is never read -- retrieval is stubbed
    -- but `analyze_document` looks the document up before it starts."""
    conn = get_db(settings)
    conn.execute(
        "INSERT INTO documents (id, path, filename, content_hash, page_count, spine_source) "
        "VALUES (1, 'data/raw/contract.pdf', 'contract.pdf', 'h', 21, 'headings')"
    )
    cursor = conn.execute(
        "INSERT INTO chunks (document_id, ordinal, content, page, page_label, section, "
        "section_path, embedding_model) VALUES (1, 0, ?, 8, '9', '6.6', ?, 'fake-hash')",
        (CLAUSE, json.dumps(["6. Identity", "6.6 Password Management Standard"])),
    )
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, document_id, embedding) VALUES (?, 1, ?)",
        (cursor.lastrowid, struct.pack(f"{DIM}f", *([0.5] * DIM))),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def searches(monkeypatch):
    """Every `search_contract` returns the one clause, whoever asks."""
    from contract_analyzer.retrieval.base import RetrievalResult

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=[make_chunk(1, CLAUSE)], candidates=20, top_k=top_k or 6)

    monkeypatch.setattr(T, "retrieve", retrieve)


# --------------------------------------------------------------------------
# The script: one criterion's three turns, five times over
# --------------------------------------------------------------------------


def draft_for(criterion) -> str:
    """A clean draft for whichever criterion is being answered. Every
    sub-requirement is met and quotes the one clause, so the validator passes
    without a correction round."""
    return json.dumps(
        {
            "compliance_question": criterion.question,
            "compliance_state": "Fully Compliant",
            "sub_requirements": [
                {
                    "id": sub.id,
                    "requirement": sub.requirement,
                    "status": "met",
                    "quote_indexes": [0],
                }
                for sub in criterion.sub_requirements
            ],
            "relevant_quotes": [{"text": "rotate credentials", "evidence_id": "E1"}],
            "rationale": "The clause is explicit.",
            "raw_confidence": 0.9,
        }
    )


def turns_for(criterion) -> list[str]:
    """The four requests a criterion makes: one search, one "done", one draft,
    and the critic's findings on it."""
    return [
        sse_message(
            [{"type": "tool_use", "id": "toolu_1", "name": "search_contract",
              "input": {"query": criterion.requirement, "mode": "hybrid"}}],
            stop_reason="tool_use",
        ),
        sse_message([{"type": "text", "text": "I have what I need."}]),
        sse_message([{"type": "text", "text": draft_for(criterion)}]),
        critic_turn(criterion),
    ]


def script(criteria=CRITERIA) -> ScriptedAPI:
    outcomes = [turn for criterion in criteria for turn in turns_for(criterion)]
    return ScriptedAPI(*outcomes)


def run(api, settings, conn, **kw):
    return analyze_document(
        1, conn, object(), settings, scripted_client(api), **kw
    )


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_every_criterion_is_analysed_and_the_report_is_in_criteria_order(settings, conn, searches):
    report = run(script(), settings, conn)

    assert report.status == "done" and report.complete
    assert [r.criterion_id for r in report.results] == [c.id for c in CRITERIA]
    assert all(r.compliance_state == "Fully Compliant" for r in report.results)
    assert report.document_id == 1 and report.filename == "contract.pdf"
    assert report.skipped == [] and report.error is None
    assert report.created_at and report.completed_at


def test_totals_sum_the_run(settings, conn, searches):
    report = run(script(), settings, conn)

    assert report.totals.criteria == 5
    assert report.totals.cost_usd == pytest.approx(sum(r.cost_usd for r in report.results))
    assert report.totals.tool_calls == 5  # one search each
    assert report.totals.needs_review == 0 and report.totals.capped == 0
    # 0.9 was the analyst's own estimate; 0.85 is what survives meeting a
    # critic that agrees. `min(analyst, critic)` -- two independent estimates
    # of one event, and the pessimist is the honest fusion.
    assert report.totals.mean_confidence == pytest.approx(0.85)
    assert report.totals.accepted == 5 and report.totals.revised == 0
    assert report.totals.evaluator_cost_usd > 0
    assert report.totals.latency_s > 0


def test_a_subset_of_criteria_can_be_asked_for(settings, conn, searches):
    wanted = [CRITERIA[2].id, CRITERIA[0].id]
    report = run(script([CRITERIA[0], CRITERIA[2]]), settings, conn, criteria=wanted)

    # Asked out of order, returned in criteria order.
    assert [r.criterion_id for r in report.results] == [CRITERIA[0].id, CRITERIA[2].id]
    assert report.totals.criteria == 2


def test_an_unknown_criterion_is_rejected_before_any_request(settings, conn, searches):
    api = script()
    with pytest.raises(KeyError, match="nonesuch"):
        run(api, settings, conn, criteria=["nonesuch"])
    assert api.calls == 0


def test_an_unknown_document_is_rejected_before_any_request(settings, conn, searches):
    api = script()
    with pytest.raises(KeyError, match="no document with id 99"):
        analyze_document(99, conn, object(), settings, scripted_client(api))
    assert api.calls == 0


def test_the_report_serialises_to_json_and_back(settings, conn, searches):
    """The report on disk is the report over the wire: no second schema."""
    from contract_analyzer.report import AnalysisReport

    report = run(script(), settings, conn)
    restored = AnalysisReport.model_validate_json(report.model_dump_json())

    assert restored == report
    assert restored.results[0].relevant_quotes[0].verified is True


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def test_every_event_carries_the_criterion_it_came_from(settings, conn, searches):
    """The agent loop emits `tool_call` with no criterion on it -- at that level
    there is only one. Five interleaved runs would be unreadable."""
    events = []
    run(script(), settings, conn, on_event=events.append)

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 5
    assert {e["criterion"] for e in tool_calls} == {c.id for c in CRITERIA}
    assert all("criterion" in e for e in events if e["type"] != "report")
    assert events[-1]["type"] == "report" and events[-1]["status"] == "done"


def test_the_callback_is_never_called_concurrently(settings, conn, searches):
    """A caller's `on_event` is invoked under the runner's lock, so the CLI can
    print and the API can fan out to its subscribers without either holding a
    lock of its own.

    The callback dawdles inside the critical section: with five criteria on
    five threads, an unserialised runner would overlap here within a few
    events.
    """
    overlaps = []
    inside = threading.Lock()
    seen = []

    def on_event(event):
        if not inside.acquire(blocking=False):
            overlaps.append(event)
            return
        try:
            seen.append(event)
            time.sleep(0.002)
        finally:
            inside.release()

    parallel = settings.model_copy(update={"analysis_workers": 5})
    run(same_answer_script(), parallel, conn, on_event=on_event)

    assert len(seen) >= 10  # five tool calls, five results, one report
    assert overlaps == []


# --------------------------------------------------------------------------
# Parallelism, cancellation, isolation
# --------------------------------------------------------------------------


def same_answer_script() -> ScriptedAPI:
    """A script whose every response is the *same* -- a search, a stop, a draft
    for whichever criterion asked, then that draft's evaluation.
    Order-independent, so five threads can race for it. `compliance_question`
    still has to match, so the reply is chosen by looking at what the request
    asked about."""

    class ByCriterion(ScriptedAPI):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()

        def __call__(self, request):
            import httpx2 as httpx

            body = json.loads(request.content or b"{}")
            with self._lock:
                self.requests.append(body)
            if "tools" not in body:
                # The critic: one turn, no tools, ever. It is the only request
                # in the run that carries no tool definitions, which is what
                # makes it identifiable without reading the prompt.
                criterion = _criterion_of(body["messages"][0]["content"])
                content = critic_turn(criterion)
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    content=content.encode(),
                )
            criterion = _criterion_of(body["system"])
            if not any(m["role"] == "user" and isinstance(m["content"], list)
                       for m in body["messages"]):
                content = turns_for(criterion)[0]           # first turn: search
            elif body.get("tool_choice") == {"type": "none"}:
                content = turns_for(criterion)[2]           # finisher: the draft
            else:
                content = turns_for(criterion)[1]           # loop: stop asking
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=content.encode()
            )

    return ByCriterion()


def _criterion_of(text: str):
    """Which criterion a request is about -- the analyst's system prompt and the
    critic's JSON request both carry the question verbatim."""
    for criterion in CRITERIA:
        if criterion.question in text:
            return criterion
    raise AssertionError("no criterion in the request")


def test_five_criteria_run_in_parallel(settings, conn, searches):
    parallel = settings.model_copy(update={"analysis_workers": 5})
    report = run(same_answer_script(), parallel, conn)

    assert [r.criterion_id for r in report.results] == [c.id for c in CRITERIA]
    assert report.totals.criteria == 5


def test_each_criterion_gets_its_own_connection(settings, conn, searches, monkeypatch):
    """`db.py`: concurrent use of one connection from two threads is a bug, and
    `check_same_thread=False` only stops sqlite3 from catching it."""
    from contract_analyzer import report as R

    opened = []
    original = R.connect

    def spy(path, **kw):
        opened.append((path, kw))
        return original(path, **kw)

    monkeypatch.setattr(R, "connect", spy)
    run(script(), settings, conn)

    assert len(opened) == 5
    assert all(kw.get("same_thread") is False for _, kw in opened)
    assert {path for path, _ in opened} == {str(settings.db_path)}


def test_the_trace_id_survives_the_pool(settings, conn, searches, tmp_path):
    """The acceptance criterion, asserted: `jq 'select(.trace_id == null)'` over
    the run's lines is empty. Without `copy_context()` every line the five
    agents emit carries a null trace, and the log stops reconstructing the run.

    Read from the JSON file rather than from `caplog`, because the context ids
    are stamped by a filter on the project's own handlers -- pytest's capture
    handler does not have it, and would show every line as untraced.
    """
    from contract_analyzer.logger import configure_logging

    log_file = tmp_path / "app.jsonl"
    configure_logging("INFO", log_file, console=False, force=True)
    try:
        with trace_context("abc123") as trace_id:
            report = run(script(), settings, conn)
    finally:
        configure_logging("INFO", None, console=False, force=True)

    assert report.trace_id == trace_id == "abc123"
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    spans = {line.get("span") for line in lines}
    assert {"analysis.document", "analysis.criterion", "agent.call", "agent.tool"} <= spans
    assert [line for line in lines if line.get("trace_id") != "abc123"] == []


def test_cancelling_skips_what_has_not_started(settings, conn, searches):
    """Cancel is checked before a criterion starts. At one worker, setting the
    flag after the first criterion leaves four skipped."""
    done = []
    cancel = False

    def cancelled():
        return cancel

    api = script()

    def on_event(event):
        nonlocal cancel
        if event["type"] == "result":
            done.append(event["criterion"])
            cancel = True

    report = analyze_document(
        1, conn, object(), settings, scripted_client(api),
        on_event=on_event, cancelled=cancelled,
    )

    assert report.status == "cancelled" and not report.complete
    assert len(report.results) == 1
    assert report.skipped == [c.id for c in CRITERIA[1:]]
    assert report.totals.criteria == 1


def test_a_cancelled_run_still_totals_what_it_finished(settings, conn, searches):
    report = analyze_document(
        1, conn, object(), settings, scripted_client(script()), cancelled=lambda: True,
    )
    assert report.status == "cancelled"
    assert report.results == [] and len(report.skipped) == 5
    assert report.totals == totals_of([], latency_s=report.totals.latency_s)
