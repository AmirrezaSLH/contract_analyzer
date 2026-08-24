"""The Analyzer's revise seam: another round on the *same* conversation.

Two modes, and the difference between them is the whole point. A `redraft`
re-reads evidence that is already in hand -- one structured call, no tools.
A `research` goes back to the index, because a redraft over evidence that
was never retrieved can only relabel, not learn.

What these tests guard is the carry-over. The ledger, the dedupe table, the
prompt-cached prefix and the tool budget all persist across a revision; a
revision that quietly started a fresh run would still produce a plausible
answer, which is exactly why it needs a test rather than a comment.
"""

from __future__ import annotations

import json

import pytest

from conftest import ScriptedAPI, make_chunk, scripted_client, sse_message
from contract_analyzer.compliance import Criterion, SubRequirement
from contract_analyzer.compliance.schemas import RevisionRequest
from contract_analyzer.config import Settings
from contract_analyzer.generation import analysis as AN
from contract_analyzer.generation import tools as T

CRITERION = Criterion(
    id="test",
    requirement="Password Rotation",
    question="Does the contract require password rotation?",
    sub_requirements=(
        SubRequirement("rotation", "Rotation at least every 90 days"),
        SubRequirement("no_default", "Default passwords prohibited"),
    ),
    states=("Fully Compliant", "Partially Compliant", "Non-Compliant"),
)

ROTATION = "Passwords shall be rotated at least every ninety (90) days."
DEFAULTS = "Supplier shall prohibit “default” passwords on every system."


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="k", log_file=None, embedding_provider="fake",
                structure_fix_rounds=2, analysis_max_tool_calls=4, analysis_effort="medium",
                research_extra_tool_calls=2)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def searches(monkeypatch):
    from contract_analyzer.retrieval.base import RetrievalResult

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=[make_chunk(1, ROTATION), make_chunk(2, DEFAULTS, page="10")],
                               candidates=20, top_k=top_k or 6)

    monkeypatch.setattr(T, "retrieve", retrieve)


def draft_json(*, state="Fully Compliant", rotation="met", no_default="met", confidence=0.9):
    return json.dumps({
        "compliance_question": CRITERION.question,
        "compliance_state": state,
        "sub_requirements": [
            {"id": "rotation", "requirement": "Rotation", "status": rotation,
             "quote_indexes": [0] if rotation in ("met", "partial") else []},
            {"id": "no_default", "requirement": "Defaults", "status": no_default,
             "quote_indexes": [1] if no_default in ("met", "partial") else []},
        ],
        "relevant_quotes": [
            {"text": ROTATION, "evidence_id": "E1"},
            {"text": DEFAULTS, "evidence_id": "E2"},
        ][: (1 if no_default not in ("met", "partial") else 2)],
        "rationale": "the clause obliges rotation",
        "raw_confidence": confidence,
    })


def search_turn(query="password rotation", call_id="toolu_1"):
    return sse_message(
        [{"type": "tool_use", "id": call_id, "name": "search_contract",
          "input": {"query": query, "mode": "keyword"}}],
        stop_reason="tool_use",
    )


def loop_prefix():
    return [search_turn(), sse_message([{"type": "text", "text": "I have what I need."}])]


def first_round(api: ScriptedAPI, s: Settings, events=None):
    return AN.analyze_criterion(
        CRITERION, None, object(), s, document_id=3, client=scripted_client(api),
        on_event=events.append if events is not None else None,
    )


def revision(mode="redraft", **kw):
    fields = dict(
        mode=mode,
        round=1,
        instructions=["relevant_quotes[1] does not support sub-requirement no_default"],
        reason_codes=["quote_unsupported"],
    )
    fields.update(kw)
    return RevisionRequest(**fields)


# -- redraft ---------------------------------------------------------------


def test_a_redraft_is_one_more_structured_call_and_asks_for_no_tools(searches):
    """The evidence is already in hand; what was disputed is the reading of it."""
    api = ScriptedAPI(
        *loop_prefix(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": draft_json(state="Partially Compliant",
                                                         no_default="missing")}]),
    )
    s = settings()
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)
    tool_calls_before = len(outcome.tools.calls)

    revised = AN.revise(outcome, revision(), settings=s, client=client)

    assert api.calls == 4
    assert len(outcome.tools.calls) == tool_calls_before  # nothing was searched
    assert revised.result.compliance_state == "Partially Compliant"
    assert revised.rounds == 1
    last = api.requests[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert last["output_config"]["format"]["type"] == "json_schema"


def test_the_redraft_continues_the_conversation_rather_than_restarting_it(searches):
    """Same prefix, same ledger, same cache. A fresh run would answer too, which
    is why this is asserted on the wire rather than on the result."""
    api = ScriptedAPI(
        *loop_prefix(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": draft_json()}]),
    )
    s = settings()
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)
    finisher_request = api.requests[-1]

    AN.revise(outcome, revision(), settings=s, client=client)
    revise_request = api.requests[-1]

    assert revise_request["system"] == finisher_request["system"]
    # The finisher's turns and its draft are in the conversation the revision
    # continues -- the draft being criticised is *there*, not paraphrased.
    assert len(revise_request["messages"]) == len(finisher_request["messages"]) + 2
    assert revise_request["messages"][-2]["role"] == "assistant"
    assert revise_request["messages"][-1]["role"] == "user"


def test_the_feedback_names_the_defect_and_never_the_answer(searches):
    """The same contract the structural fix rounds keep. A revision that said
    'mark no_default missing' would make the next draft the Router's."""
    api = ScriptedAPI(
        *loop_prefix(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": draft_json()}]),
    )
    s = settings()
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)
    AN.revise(outcome, revision(), settings=s, client=client)

    feedback = api.requests[-1]["messages"][-1]["content"]
    assert "relevant_quotes[1] does not support sub-requirement no_default" in feedback
    for state in ("Fully Compliant", "Partially Compliant", "Non-Compliant"):
        assert state not in feedback


# -- research --------------------------------------------------------------


def test_research_re_enters_the_loop_with_tools_offered_again(searches):
    api = ScriptedAPI(
        *loop_prefix(),
        sse_message([{"type": "text", "text": draft_json()}]),
        search_turn("privileged password vaulting", "toolu_2"),
        sse_message([{"type": "text", "text": "Now I have it."}]),
        sse_message([{"type": "text", "text": draft_json()}]),
    )
    s = settings()
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)

    revised = AN.revise(outcome, revision(mode="research"), settings=s, client=client)

    assert len(revised.tools.calls) == 2  # the first leg's one, plus one more
    resumed = api.requests[3]
    assert "tool_choice" not in resumed  # tools offered, not suppressed
    assert [t["name"] for t in resumed["tools"]] == ["search_contract", "get_section"]


def test_research_grants_a_delta_and_not_a_fresh_allowance(searches):
    """A first leg that used its whole budget gets `research_extra_tool_calls`
    more -- not another `analysis_max_tool_calls`. The counter is absolute and
    counted against calls already made."""
    s = settings(analysis_max_tool_calls=2, research_extra_tool_calls=1)
    api = ScriptedAPI(
        search_turn(call_id="toolu_1"),
        search_turn("default passwords", "toolu_2"),
        sse_message([{"type": "text", "text": draft_json()}]),
        # The revision leg: one more search is allowed, then the cap bites.
        search_turn("vaulting", "toolu_3"),
        sse_message([{"type": "text", "text": draft_json()}]),
    )
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)
    assert outcome.result.ended_by == "cap" and len(outcome.tools.calls) == 2

    revised = AN.revise(outcome, revision(mode="research"), settings=s, client=client)

    assert len(revised.tools.calls) == 3
    assert revised.run.ended_by == "cap"
    assert not api.outcomes, "the script should be exhausted -- no extra leg ran"


def test_the_ledger_is_the_same_object_across_a_revision(searches):
    """The Router slices cited passages out of this ledger to build the
    Evaluator's request. A copy would hand the critic text that was never
    verified against what the Analyzer actually read."""
    api = ScriptedAPI(
        *loop_prefix(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": draft_json()}]),
    )
    s = settings()
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)
    ledger = outcome.evidence

    revised = AN.revise(outcome, revision(), settings=s, client=client)

    assert revised.evidence is ledger
    assert revised.tools is outcome.tools


def test_a_revision_announces_itself_so_the_progress_view_can_show_it(searches):
    api = ScriptedAPI(
        *loop_prefix(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": "Nothing further to search."}]),
        sse_message([{"type": "text", "text": draft_json()}]),
    )
    s = settings()
    client = scripted_client(api)
    outcome = AN.analyze_criterion(CRITERION, None, object(), s, document_id=3, client=client)
    events = []

    AN.revise(outcome, revision(mode="research"), settings=s, client=client,
              on_event=events.append)

    revising = [e for e in events if e["type"] == "revising"]
    assert len(revising) == 1
    assert revising[0]["mode"] == "research" and revising[0]["round"] == 1
    assert revising[0]["reasons"] == ["quote_unsupported"]
