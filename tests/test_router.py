"""The Router: the decision policy, the isolation it guarantees, the fan-in.

The policy is a pure function over structured judgements, so most of this
file is a table. The parts that are not a table are the two properties the
whole design rests on. **Isolation**: the request the Evaluator receives
carries only the evidence the draft actually cited, and none of the
conversation -- asserted here on real outcomes, where `test_agent_protocol.py`
asserted it on the schema. **One-directional failure**: an Evaluator that
cannot answer lowers what ships and never stops it.
"""

from __future__ import annotations

import json

import pytest

from conftest import ScriptedAPI, make_chunk, scripted_client, sse_message
from contract_analyzer.compliance import Criterion, SubRequirement
from contract_analyzer.compliance.schemas import ComplianceResult, EvaluatorFindings
from contract_analyzer.config import Settings
from contract_analyzer.generation import router as R
from contract_analyzer.generation import tools as T

CRITERION = Criterion(
    id="c1",
    requirement="Password Management",
    question="Does the contract require password management controls?",
    sub_requirements=(
        SubRequirement("rotation", "Passwords are rotated at least every 90 days"),
        SubRequirement("vaulting", "Privileged credentials are held in a vault"),
    ),
    states=("Fully Compliant", "Partially Compliant", "Non-Compliant"),
)

ROTATION = "Passwords shall be rotated at least every ninety (90) days."
VAULTING = "Privileged credentials shall be held in an approved vault."


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="k", log_file=None, embedding_provider="fake",
                structure_fix_rounds=2, analysis_max_tool_calls=4, analysis_effort="medium",
                evaluator_model="claude-sonnet-5", router_max_rounds=1)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def searches(monkeypatch):
    from contract_analyzer.retrieval.base import RetrievalResult

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        return RetrievalResult(
            question=question, mode=mode or "hybrid", document_id=document_id,
            chunks=[make_chunk(1, ROTATION), make_chunk(2, VAULTING, page="10")],
            candidates=20, top_k=top_k or 6,
        )

    monkeypatch.setattr(T, "retrieve", retrieve)


# -- scripted turns --------------------------------------------------------


def draft_json(*, state="Fully Compliant", rotation="met", vaulting="met", confidence=0.9):
    statuses = {"rotation": rotation, "vaulting": vaulting}
    quotes, indexes = [], {}
    for sub_id, text, evidence in (("rotation", ROTATION, "E1"), ("vaulting", VAULTING, "E2")):
        if statuses[sub_id] in ("met", "partial"):
            indexes[sub_id] = [len(quotes)]
            quotes.append({"text": text, "evidence_id": evidence})
        else:
            indexes[sub_id] = []
    return json.dumps({
        "compliance_question": CRITERION.question,
        "compliance_state": state,
        "sub_requirements": [
            {"id": "rotation", "requirement": "Passwords are rotated at least every 90 days",
             "status": rotation, "quote_indexes": indexes["rotation"]},
            {"id": "vaulting", "requirement": "Privileged credentials are held in a vault",
             "status": vaulting, "quote_indexes": indexes["vaulting"]},
        ],
        "relevant_quotes": quotes,
        "rationale": "the clauses oblige both",
        "raw_confidence": confidence,
    })


def findings_json(**overrides) -> str:
    fields = dict(
        quote_support=[
            {"quote_index": 0, "sub_requirement_id": "rotation", "support": "supports",
             "note": "obliges rotation"},
            {"quote_index": 1, "sub_requirement_id": "vaulting", "support": "supports",
             "note": "obliges vaulting"},
        ],
        status_agreement=[
            {"sub_requirement_id": "rotation", "agreement": "agree", "note": ""},
            {"sub_requirement_id": "vaulting", "agreement": "agree", "note": ""},
        ],
        state_agreement="agree",
        missing_searches=[],
        critic_confidence=0.8,
        notes="",
    )
    fields.update(overrides)
    return json.dumps(fields)


def findings(**overrides) -> EvaluatorFindings:
    return EvaluatorFindings.model_validate_json(findings_json(**overrides))


def loop_turns():
    """One search, then the model says it is done."""
    return [
        sse_message([{"type": "tool_use", "id": "toolu_1", "name": "search_contract",
                      "input": {"query": "password rotation vault", "mode": "keyword"}}],
                    stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "I have what I need."}]),
    ]


def route(api: ScriptedAPI, s: Settings | None = None, events=None) -> ComplianceResult:
    return R.route_criterion(
        CRITERION, None, object(), s or settings(), document_id=3,
        client=scripted_client(api), on_event=events.append if events is not None else None,
    )


# ==========================================================================
# The decision policy
# ==========================================================================


def test_clean_findings_are_accepted_with_no_revision():
    decision = R.decide(findings(), 0, settings())
    assert decision.verdict == "accept"
    assert decision.reasons == [] and decision.instructions == []


@pytest.mark.parametrize("support", ["irrelevant", "contradicts"])
def test_a_quote_that_does_not_carry_its_claim_is_a_redraft(support):
    """The evidence is in hand; what was wrong was the reading of it."""
    decision = R.decide(
        findings(quote_support=[{"quote_index": 1, "sub_requirement_id": "vaulting",
                                 "support": support, "note": "this clause is about backups"}]),
        0, settings(),
    )
    assert decision.verdict == "revise" and decision.mode == "redraft"
    assert decision.reasons == [f"quote_{support}"]
    assert "relevant_quotes[1]" in decision.instructions[0]
    assert "vaulting" in decision.instructions[0]


def test_a_partial_quote_is_not_a_defect():
    """`partial` is a gap in the contract, not an error in the assessment. If it
    triggered a revision every hedged clause would cost an extra round."""
    decision = R.decide(
        findings(quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                                 "support": "partial", "note": "best-efforts wording"}]),
        0, settings(),
    )
    assert decision.verdict == "accept"


def test_an_unsearched_missing_verdict_is_a_research_round():
    decision = R.decide(findings(missing_searches=["vaulting"]), 0, settings())
    assert decision.verdict == "revise" and decision.mode == "research"
    assert decision.reasons == ["unsearched_requirement"]
    assert "search for it" in decision.instructions[0]


def test_research_outranks_redraft_when_both_apply():
    """A redraft over evidence that was never retrieved can only relabel."""
    decision = R.decide(
        findings(
            quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                            "support": "irrelevant", "note": ""}],
            missing_searches=["vaulting"],
        ),
        0, settings(),
    )
    assert decision.mode == "research"
    assert set(decision.reasons) == {"quote_irrelevant", "unsearched_requirement"}


def test_a_disputed_status_or_state_is_a_redraft():
    decision = R.decide(
        findings(
            status_agreement=[{"sub_requirement_id": "rotation", "agreement": "too_strong",
                               "note": "the clause only permits"}],
            state_agreement="disagree",
        ),
        0, settings(),
    )
    assert decision.verdict == "revise" and decision.mode == "redraft"
    assert decision.reasons == ["status_too_strong", "state_disagreement"]


def test_rounds_exhausted_falls_back_rather_than_looping():
    open_findings = findings(missing_searches=["vaulting"])
    assert R.decide(open_findings, 0, settings(router_max_rounds=1)).verdict == "revise"
    assert R.decide(open_findings, 1, settings(router_max_rounds=1)).verdict == "fallback"
    # Zero rounds configured means the critic still runs and still lowers the
    # score -- it just never asks for the answer to be redone.
    assert R.decide(open_findings, 0, settings(router_max_rounds=0)).verdict == "fallback"


def test_the_instructions_name_the_defect_and_never_the_answer():
    decision = R.decide(
        findings(
            quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                            "support": "irrelevant", "note": "about physical access"}],
            state_agreement="disagree",
        ),
        0, settings(),
    )
    told = " ".join(decision.instructions)
    for answer in ("Fully Compliant", "Partially Compliant", "Non-Compliant",
                   "mark it missing", "status should be"):
        assert answer not in told


# ==========================================================================
# What the Evaluator is allowed to see
# ==========================================================================


def test_the_request_carries_only_the_evidence_the_draft_cited(searches):
    """Both passages were retrieved; the draft quotes one. The critic sees one:
    a passage no claim rests on is not evidence for a claim nobody made, and
    offering it invites the critic to write the assessment instead of check it.
    """
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json(state="Partially Compliant",
                                                         vaulting="missing")}]),
    )
    from contract_analyzer.generation.analysis import analyze_criterion

    outcome = analyze_criterion(CRITERION, None, object(), settings(), document_id=3,
                                client=scripted_client(api))
    request = R.build_evaluation_request(outcome, round=0)

    assert len(outcome.evidence) == 2
    assert [p.evidence_id for p in request.passages] == ["E1"]
    assert request.searched_queries == ["password rotation vault"]
    assert request.round == 0


def test_the_request_carries_no_trace_of_the_conversation(searches):
    api = ScriptedAPI(*loop_turns(), sse_message([{"type": "text", "text": draft_json()}]))
    from contract_analyzer.generation.analysis import analyze_criterion

    outcome = analyze_criterion(CRITERION, None, object(), settings(), document_id=3,
                                client=scripted_client(api))
    body = json.dumps(R.build_evaluation_request(outcome, round=0).model_dump(mode="json"))

    assert "tool_use" not in body and "tool_result" not in body
    assert "I have what I need" not in body  # the analyst's own words about its work


# ==========================================================================
# The loop, end to end
# ==========================================================================


def test_an_accepted_criterion_runs_one_analysis_and_one_critic_call(searches):
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": findings_json()}]),
    )
    events = []
    result = route(api, events=events)

    assert api.calls == 4
    assert result.verdict == "accept" and result.rounds == 0
    assert not result.needs_review
    assert result.evaluator_findings is not None
    assert [e["type"] for e in events][-3:] == ["evaluating", "decision", "result"]
    assert events[-1]["verdict"] == "accept"


def test_a_disputed_quote_costs_exactly_one_revision_then_is_accepted(searches):
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": findings_json(
            quote_support=[{"quote_index": 1, "sub_requirement_id": "vaulting",
                            "support": "irrelevant", "note": "about backups"}])}]),
        sse_message([{"type": "text", "text": draft_json(state="Partially Compliant",
                                                         vaulting="missing")}]),
        sse_message([{"type": "text", "text": findings_json(
            quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                            "support": "supports", "note": ""}])}]),
    )
    events = []
    result = route(api, events=events)

    assert result.verdict == "accept" and result.rounds == 1
    assert result.compliance_state == "Partially Compliant"
    assert [e["type"] for e in events].count("revising") == 1
    assert [e["type"] for e in events].count("evaluating") == 2


def test_findings_still_open_when_the_rounds_run_out_fall_back_flagged(searches):
    disputed = findings_json(
        quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                        "support": "contradicts", "note": "says the opposite"}],
        critic_confidence=0.4,
    )
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": disputed}]),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": disputed}]),
    )
    result = route(api)

    assert result.verdict == "fallback"
    assert result.needs_review and result.confidence <= 0.5
    assert result.rounds == 1
    assert result.evaluator_findings is not None  # the reason is attached, not just the flag


def test_a_fallback_ships_the_best_round_not_the_last(searches):
    """Found by a live run: a redraft came back degenerate -- a garbled
    evidence id, six sub-requirements lost -- and shipped anyway because it
    was newest. The round with fewer unresolved errors is the one that ships;
    the rounds spent stay on the result, because the loop was paid for."""
    broken = json.loads(draft_json(state="Partially Compliant", vaulting="partial"))
    broken["relevant_quotes"][1]["evidence_id"] = "E9"  # not a retrieved passage
    disputed_round_0 = findings_json(
        quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                        "support": "contradicts", "note": "says the opposite"}],
        critic_confidence=0.5,
    )
    disputed_round_1 = findings_json(
        quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                        "support": "contradicts", "note": "still the opposite"}],
        critic_confidence=0.3,
    )
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": disputed_round_0}]),
        sse_message([{"type": "text", "text": json.dumps(broken)}]),
        sse_message([{"type": "text", "text": disputed_round_1}]),
    )
    result = route(api, settings(structure_fix_rounds=0))

    assert result.verdict == "fallback"
    assert result.compliance_state == "Fully Compliant"  # round 0's draft, not the broken one
    assert result.unresolved_errors == []
    assert result.rounds == 1  # the revision still happened and is still counted
    assert result.confidence_components["critic"] == 0.5  # round 0's critic, to match
    assert result.needs_review


def test_a_failed_critics_spend_is_still_booked(searches):
    """The money is spent whether or not an answer arrives. Three unusable
    critic attempts must show up in the criterion's cost and tokens, or the
    KPI totals report a smaller bill than the one that was paid."""
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        *[sse_message([{"type": "text", "text": "not findings"}],
                      input_tokens=1200, output_tokens=300) for _ in range(3)],
    )
    result = route(api)

    assert result.verdict == "unevaluated"
    assert result.evaluator_cost_usd > 0
    assert result.cost_usd > result.evaluator_cost_usd  # analysis + the failed critic
    assert result.usage["input_tokens"] >= 3 * 1200


def test_an_evaluator_that_cannot_answer_lowers_the_result_but_never_blocks_it(searches):
    """The whole failure strategy in one test. Three unusable critic answers,
    and the analysis still ships -- flagged `unevaluated`, capped, with the
    analyst's own confidence rather than a fabricated agreement."""
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        *[sse_message([{"type": "text", "text": "not findings"}]) for _ in range(3)],
    )
    result = route(api)

    assert result.verdict == "unevaluated"
    assert result.needs_review and result.confidence <= 0.5
    assert result.evaluator_findings is None
    assert result.compliance_state == "Fully Compliant"  # the analysis survived intact
    assert "critic" not in result.confidence_components


def test_the_critic_call_is_paid_for_out_of_the_criterions_own_cost(searches):
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json()}]),
        sse_message([{"type": "text", "text": findings_json()}],
                    input_tokens=1500, output_tokens=250),
    )
    result = route(api)
    assert result.evaluator_cost_usd > 0
    assert result.cost_usd > result.evaluator_cost_usd


def test_the_composed_confidence_takes_the_pessimist_of_the_two_estimates(searches):
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json(confidence=0.9)}]),
        sse_message([{"type": "text", "text": findings_json(critic_confidence=0.55)}]),
    )
    result = route(api)
    assert result.raw_confidence == 0.9
    assert result.confidence_components["critic"] == 0.55
    assert result.confidence == pytest.approx(0.55)


def test_partial_evidence_for_a_partial_claim_does_not_cost_the_score(searches):
    """Found by running the pipeline against the real API: the critic agreed
    with every status and every quote, and the confidence still came out at
    half, because `partial` support was scored as a shortfall even where
    `partial` was exactly what had been claimed."""
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json(
            state="Partially Compliant", rotation="partial", vaulting="partial",
            confidence=0.8)}]),
        sse_message([{"type": "text", "text": findings_json(
            quote_support=[
                {"quote_index": 0, "sub_requirement_id": "rotation",
                 "support": "partial", "note": "carve-out"},
                {"quote_index": 1, "sub_requirement_id": "vaulting",
                 "support": "partial", "note": "hedged"},
            ],
            critic_confidence=0.85)}]),
    )
    result = route(api)

    assert result.verdict == "accept"
    assert result.confidence_components["quote_term"] == 1.0
    assert result.confidence == pytest.approx(0.8)


def test_partial_evidence_for_a_met_claim_still_costs_the_score(searches):
    """The other half of the same rule: `partial` support under a `met` status
    is the shortfall the quote term exists to measure."""
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json(confidence=0.8)}]),
        sse_message([{"type": "text", "text": findings_json(
            quote_support=[
                {"quote_index": 0, "sub_requirement_id": "rotation",
                 "support": "partial", "note": "only permits"},
                {"quote_index": 1, "sub_requirement_id": "vaulting",
                 "support": "supports", "note": "obliges"},
            ],
            critic_confidence=0.85)}]),
    )
    result = route(api)
    assert result.confidence_components["quote_term"] == 0.75


def test_a_critic_reading_a_different_state_costs_the_score_but_not_the_verdict(searches):
    api = ScriptedAPI(
        *loop_turns(),
        sse_message([{"type": "text", "text": draft_json(confidence=0.9)}]),
        sse_message([{"type": "text", "text": findings_json(state_agreement="disagree")}]),
        sse_message([{"type": "text", "text": draft_json(confidence=0.9)}]),
        sse_message([{"type": "text", "text": findings_json(state_agreement="disagree")}]),
    )
    result = route(api)
    assert result.compliance_state == "Fully Compliant"  # the Router does not overrule
    assert result.confidence_components["agreement"] == 0.6
    assert result.verdict == "fallback"


# ==========================================================================
# After fan-in
# ==========================================================================


def result_for(criterion_id, *, sub_statuses, quotes=()) -> ComplianceResult:
    return ComplianceResult(
        criterion_id=criterion_id,
        compliance_requirement="Password Management",
        compliance_question="?",
        compliance_state="Partially Compliant",
        sub_requirements=[
            {"id": sub_id, "requirement": text, "status": status, "quote_indexes": []}
            for sub_id, text, status in sub_statuses
        ],
        relevant_quotes=[
            {"text": text, "evidence_id": "E1", "section_ref": section, "page_display": "12",
             "chunk_id": 4, "verified": True}
            for text, section in quotes
        ],
        rationale="", raw_confidence=0.8, confidence=0.8, confidence_components={},
        needs_review=False, structure_rounds=0, ended_by="model", tool_calls=1,
        usage={}, cost_usd=0.0, model="claude-sonnet-5",
    )


def test_one_criterion_finding_what_another_called_missing_becomes_a_note():
    """Neither run could have seen this: the five run in parallel and never meet."""
    notes = R.cross_criterion_check([
        result_for("c1", sub_statuses=[
            ("vaulting", "Privileged credentials are held in a vault", "missing"),
            ("rotation", "Passwords are rotated every 90 days", "met"),
        ]),
        result_for("c3", sub_statuses=[("storage", "Credentials at rest", "met")],
                   quotes=[("Privileged credentials shall be held in an approved vault.",
                            "Exhibit G")]),
    ])
    assert len(notes) == 1
    assert "c1 marked 'vaulting' missing" in notes[0]
    assert "c3 quotes Exhibit G, p.12" in notes[0]


def test_a_criterion_is_not_reported_against_itself():
    notes = R.cross_criterion_check([
        result_for("c1",
                   sub_statuses=[("vaulting", "Privileged credentials in a vault", "missing")],
                   quotes=[("Privileged credentials shall be vaulted.", "6.6")]),
    ])
    assert notes == []


def test_agreeing_criteria_produce_no_notes():
    notes = R.cross_criterion_check([
        result_for("c1", sub_statuses=[("vaulting", "Privileged credentials in a vault",
                                        "missing")]),
        result_for("c2", sub_statuses=[("tls", "TLS 1.2 in transit", "met")],
                   quotes=[("All traffic shall use TLS 1.2 or higher.", "8.1")]),
    ])
    assert notes == []
