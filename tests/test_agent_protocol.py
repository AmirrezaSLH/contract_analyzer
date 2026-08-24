"""The three-agent protocol: the message models, and what they refuse to carry.

Every hand-off between the Router, the Analyzer and the Evaluator is one of
these models. Two properties are worth pinning in a test rather than a
comment. The first is **compatibility**: a report written before the
Evaluator existed still parses, and reads honestly when it does. The second
is **isolation**: `EvaluationRequest` has nowhere to put the Analyzer's
conversation, so a critic cannot inherit the reasoning that made the error
even by accident. That is a schema-level guarantee, and this is where it is
asserted; `test_router.py` asserts the Router honours it on real outcomes.
"""

from __future__ import annotations

import json

import pytest

from contract_analyzer.compliance.schemas import (
    ComplianceResult,
    EvaluationRequest,
    EvaluatorFindings,
    RevisionRequest,
)


def findings(**overrides) -> EvaluatorFindings:
    fields = dict(
        quote_support=[
            {"quote_index": 0, "sub_requirement_id": "rotation",
             "support": "supports", "note": "obliges rotation every 90 days"},
        ],
        status_agreement=[
            {"sub_requirement_id": "rotation", "agreement": "agree", "note": "follows"},
        ],
        state_agreement="agree",
        missing_searches=[],
        critic_confidence=0.8,
        notes="",
    )
    fields.update(overrides)
    return EvaluatorFindings.model_validate(fields)


# -- compatibility ---------------------------------------------------------


def test_a_result_written_before_the_evaluator_still_parses():
    """The new fields default; the default reads honestly rather than optimistically."""
    old = {
        "criterion_id": "c1",
        "compliance_requirement": "Password Rotation",
        "compliance_question": "Does it?",
        "compliance_state": "Fully Compliant",
        "sub_requirements": [],
        "relevant_quotes": [],
        "rationale": "because",
        "raw_confidence": 0.9,
        "confidence": 0.8,
        "confidence_components": {"raw": 0.9},
        "needs_review": False,
        "structure_rounds": 0,
        "ended_by": "model",
        "tool_calls": 3,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "cost_usd": 0.01,
        "model": "claude-sonnet-5",
    }
    result = ComplianceResult.model_validate(old)
    # Not "accept": a run nobody evaluated must not claim it passed evaluation.
    assert result.verdict == "unevaluated"
    assert result.rounds == 0
    assert result.evaluator_findings is None


def test_findings_round_trip_through_json_unchanged():
    """The seam is only real if it survives serialisation -- an out-of-process
    evaluator hands back exactly this."""
    original = findings(notes="the vaulting quote is about a different system")
    assert EvaluatorFindings.model_validate_json(original.model_dump_json()) == original


# -- the structured-output schema ------------------------------------------


def test_the_findings_schema_is_closed_and_fully_required():
    """What the grammar needs: no extra keys, nothing optional, at every level."""
    schema = EvaluatorFindings.output_format()
    assert schema["type"] == "json_schema"
    bodies = [schema["schema"], *schema["schema"].get("$defs", {}).values()]
    for body in bodies:
        if body.get("type") != "object":
            continue
        assert body["additionalProperties"] is False
        assert sorted(body["required"]) == sorted(body["properties"])


def test_every_enum_the_critic_may_answer_is_in_the_schema():
    """Constrained decoding is what makes the support values trustworthy; if one
    were free text the deterministic checks downstream would be reading prose."""
    defs = EvaluatorFindings.output_format()["schema"]["$defs"]
    assert set(defs["QuoteSupport"]["properties"]["support"]["enum"]) == {
        "supports", "partial", "irrelevant", "contradicts",
    }
    assert set(defs["StatusAgreement"]["properties"]["agreement"]["enum"]) == {
        "agree", "too_strong", "too_weak",
    }


# -- what the findings compute ---------------------------------------------


@pytest.mark.parametrize(
    ("supports", "expected"),
    [
        (["supports", "supports"], 1.0),
        (["supports", "irrelevant"], 0.5),
        (["partial", "partial"], 0.5),
        (["supports", "partial"], 0.75),
        (["contradicts"], 0.0),
    ],
)
def test_partial_support_for_an_unnamed_claim_scores_a_half(supports, expected):
    """Without the statuses to compare against, `partial` is read
    conservatively. The Router always has them; this is the fallback."""
    judged = findings(
        quote_support=[
            {"quote_index": i, "sub_requirement_id": "rotation", "support": s, "note": ""}
            for i, s in enumerate(supports)
        ]
    )
    assert judged.support_ratio() == pytest.approx(expected)


def test_partial_support_for_a_partial_claim_is_agreement_and_costs_nothing():
    """The bug this exists to prevent, found by running the pipeline for real:
    a criterion where the critic agreed with every status and every quote still
    scored 0.36, because seven `partial` readings of seven `partial` claims were
    counted as half-failures. Reading a hedged clause correctly and saying so is
    the right answer, not a discount."""
    judged = findings(
        quote_support=[
            {"quote_index": 0, "sub_requirement_id": "rotation", "support": "partial",
             "note": "carve-out for technical necessity"},
        ]
    )
    assert judged.support_ratio({"rotation": "partial"}) == 1.0
    # But the same reading under a `met` claim is exactly the shortfall the
    # term is for: the language does not carry the obligation claimed.
    assert judged.support_ratio({"rotation": "met"}) == 0.5


def test_support_ratio_of_a_draft_that_claimed_nothing_is_one():
    """A Non-Compliant verdict quotes nothing. Dividing by zero claims would
    make "found no language" indistinguishable from "quoted badly"."""
    assert findings(quote_support=[]).support_ratio() == 1.0


def test_disputed_reads_only_the_two_values_that_mean_the_quote_failed():
    """`partial` is a gap, not a defect: it must not trigger a revision on its own."""
    judged = findings(
        quote_support=[
            {"quote_index": 0, "sub_requirement_id": "a", "support": "partial", "note": ""},
            {"quote_index": 1, "sub_requirement_id": "b", "support": "irrelevant", "note": ""},
            {"quote_index": 2, "sub_requirement_id": "c", "support": "contradicts", "note": ""},
        ],
        status_agreement=[
            {"sub_requirement_id": "a", "agreement": "agree", "note": ""},
            {"sub_requirement_id": "b", "agreement": "too_strong", "note": ""},
        ],
    )
    assert [q.quote_index for q in judged.disputed_quotes] == [1, 2]
    assert [s.sub_requirement_id for s in judged.disputed_statuses] == ["b"]


# -- isolation, at the schema level ----------------------------------------


def test_the_evaluation_request_has_nowhere_to_put_the_conversation():
    """The Evaluator's blindness is structural, not a convention the Router
    remembers to follow. Adding `messages` here would be the bug this catches."""
    forbidden = {"messages", "conversation", "thinking", "system", "evidence", "tools"}
    assert forbidden.isdisjoint(EvaluationRequest.model_fields)


def test_the_request_serialises_to_the_json_an_out_of_process_critic_would_get():
    request = EvaluationRequest(
        criterion_id="c1",
        requirement="Password Rotation",
        question="Does it?",
        compliance_state="Partially Compliant",
        sub_requirements=[
            {"id": "rotation", "requirement": "Rotate", "status": "met", "quote_indexes": [0]},
        ],
        quotes=[{"index": 0, "text": "shall be rotated", "evidence_id": "E1",
                 "hedge_terms": []}],
        passages=[{"evidence_id": "E1", "section_path": "6.6 Passwords", "page": "9",
                   "text": "Passwords shall be rotated."}],
        rationale="the clause obliges rotation",
        searched_queries=["password rotation"],
        unsearched=[],
        round=0,
    )
    payload = json.loads(request.model_dump_json())
    assert payload["passages"][0]["evidence_id"] == "E1"
    assert payload["round"] == 0


# -- what the Analyzer is told ---------------------------------------------


def test_a_revision_request_renders_its_instructions_as_a_bullet_list():
    revision = RevisionRequest(
        mode="redraft",
        round=1,
        instructions=[
            "relevant_quotes[1] does not support sub-requirement mfa",
            "sub_requirements[0] status is stronger than its quotes carry",
        ],
        reason_codes=["quote_unsupported", "status_too_strong"],
    )
    assert revision.text() == (
        "- relevant_quotes[1] does not support sub-requirement mfa\n"
        "- sub_requirements[0] status is stronger than its quotes carry"
    )
