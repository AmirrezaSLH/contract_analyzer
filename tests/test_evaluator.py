"""The Evaluator: the deterministic half, the critic call, and the ladder.

Three things are worth a test here. **E1 facts survive**: a `missing` verdict
nobody searched for is reported whether or not the model ever answers, because
it is a fact about the log rather than a judgement. **Findings are checked in
Python**: constrained decoding guarantees the shape of the answer and nothing
about whether its indexes refer to anything. And **failure is graceful and
one-directional**: the ladder backs off, then gives up in a way the Router can
degrade -- the Evaluator lowers what ships and is never the reason nothing does.
"""

from __future__ import annotations

import json

import pytest

from conftest import ScriptedAPI, scripted_client, sse_message
from contract_analyzer.compliance import Criterion, SubRequirement
from contract_analyzer.compliance.schemas import EvaluationRequest, EvaluatorFindings
from contract_analyzer.config import Settings
from contract_analyzer.generation import evaluator as EV

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


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="k", log_file=None, embedding_provider="fake",
                evaluator_model="claude-sonnet-5", evaluator_effort="medium")
    base.update(kw)
    return Settings(**base)


def request(**overrides) -> EvaluationRequest:
    fields = dict(
        criterion_id="c1",
        requirement=CRITERION.requirement,
        question=CRITERION.question,
        compliance_state="Partially Compliant",
        sub_requirements=[
            {"id": "rotation", "requirement": "Passwords are rotated at least every 90 days",
             "status": "met", "quote_indexes": [0]},
            {"id": "vaulting", "requirement": "Privileged credentials are held in a vault",
             "status": "missing", "quote_indexes": []},
        ],
        quotes=[{"index": 0, "text": "Passwords shall be rotated every ninety (90) days.",
                 "evidence_id": "E1", "hedge_terms": []}],
        passages=[{"evidence_id": "E1", "section_path": "6.6 Passwords", "page": "9",
                   "text": "Passwords shall be rotated every ninety (90) days."}],
        rationale="rotation is obliged; no vaulting language found",
        searched_queries=["password rotation schedule"],
        unsearched=[],
        round=0,
    )
    fields.update(overrides)
    return EvaluationRequest.model_validate(fields)


def findings_json(**overrides) -> str:
    fields = dict(
        quote_support=[{"quote_index": 0, "sub_requirement_id": "rotation",
                        "support": "supports", "note": "obliges rotation"}],
        status_agreement=[
            {"sub_requirement_id": "rotation", "agreement": "agree", "note": "follows"},
            {"sub_requirement_id": "vaulting", "agreement": "agree", "note": "nothing found"},
        ],
        state_agreement="agree",
        missing_searches=[],
        critic_confidence=0.7,
        notes="",
    )
    fields.update(overrides)
    return json.dumps(fields)


def evaluate(api: ScriptedAPI, req=None, s=None, sleeps=None):
    s = s or settings()
    return EV.evaluate(
        req if req is not None else request(), CRITERION,
        settings=s, client=scripted_client(api),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


# ==========================================================================
# E1 -- the deterministic pre-checks
# ==========================================================================


def test_a_missing_verdict_nobody_searched_for_is_reported_as_a_fact():
    """`vaulting` was called missing and no query went near it. The critic below
    says nothing about it; the fact reaches the Router anyway."""
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    evaluation = evaluate(api)
    assert evaluation.request.unsearched == ["vaulting"]
    assert evaluation.findings.missing_searches == ["vaulting"]


def test_a_missing_verdict_that_was_searched_for_is_not_flagged():
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    evaluation = evaluate(
        api, request(searched_queries=["password rotation", "privileged credential vault"])
    )
    assert evaluation.request.unsearched == []
    assert evaluation.findings.missing_searches == []


def test_the_coverage_check_looks_for_what_separates_a_sub_requirement_from_its_siblings():
    """Every sub-requirement here contains "password". If the check accepted any
    shared word it would call all of them searched and never fire at all."""
    terms = EV.distinctive_terms(
        CRITERION.requirement, CRITERION.sub_requirements, "vaulting"
    )
    assert terms == {"privileg", "credential", "held", "vault"}
    assert "password" not in terms


@pytest.mark.parametrize(
    ("requirement", "query", "searched"),
    [
        ("Passwords are rotated at least every 90 days", "password rotation policy", True),
        ("Privileged credentials are held in a vault", "password rotation policy", False),
        # Stemming, not vocabulary matching: the analyst wrote one form, the
        # criterion another, and calling that "never searched" would be a false
        # accusation dressed up as a fact about the log.
        ("Multi-factor authentication is enforced", "enforcing multi factor auth", True),
    ],
)
def test_search_coverage_survives_the_shapes_the_same_word_takes(requirement, query, searched):
    criterion = Criterion(
        id="x", requirement="Controls", question="?",
        sub_requirements=(SubRequirement("only", requirement),),
        states=("Fully Compliant",),
    )
    assert EV.searched_for(
        EV.distinctive_terms(criterion.requirement, criterion.sub_requirements, "only"),
        [query],
    ) is searched


def test_a_hedged_quote_under_a_met_status_reaches_the_critic_flagged():
    """A flag, not a verdict -- the lexicon cannot tell a carve-out from a
    subordinate clause, and does not try."""
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    hedged = request(
        quotes=[{"index": 0, "evidence_id": "E1", "hedge_terms": [],
                 "text": "Supplier may use commercially reasonable efforts to rotate."}],
    )
    evaluation = evaluate(api, hedged)
    assert evaluation.request.quotes[0].hedge_terms == [
        "commercially reasonable", "reasonable efforts", "may",
    ]
    # And the flag is in the JSON the critic actually saw.
    sent = api.requests[0]["messages"][0]["content"]
    assert "commercially reasonable" in sent


def test_a_quote_under_a_missing_status_is_not_hedge_flagged():
    """Hedging only means something where an obligation was claimed."""
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    evaluation = evaluate(api, request(
        sub_requirements=[
            {"id": "rotation", "requirement": "Passwords are rotated at least every 90 days",
             "status": "missing", "quote_indexes": []},
            {"id": "vaulting", "requirement": "Privileged credentials are held in a vault",
             "status": "missing", "quote_indexes": []},
        ],
        quotes=[{"index": 0, "evidence_id": "E1", "hedge_terms": [],
                 "text": "Supplier may rotate passwords."}],
        searched_queries=["password rotation", "privileged credential vault"],
    ))
    assert evaluation.request.quotes[0].hedge_terms == []


def test_precheck_does_not_mutate_what_the_router_built():
    original = request()
    enriched = EV.precheck(original, CRITERION)
    assert original.unsearched == [] and enriched.unsearched == ["vaulting"]


# ==========================================================================
# What the critic sees
# ==========================================================================


def test_the_critic_gets_the_request_as_json_and_no_tools():
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    evaluate(api)
    sent = api.requests[0]
    assert "tools" not in sent
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["model"] == "claude-sonnet-5"
    body = sent["messages"][0]["content"]
    assert '"evidence_id": "E1"' in body
    assert "6.6 Passwords" in body


def test_the_critic_is_never_shown_the_analyst_conversation():
    """The isolation the whole design rests on, asserted on the wire this time."""
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    evaluate(api)
    body = json.dumps(api.requests[0])
    for leak in ("tool_use", "tool_result", "search_contract", "thinking"):
        assert leak not in body


def test_the_evaluator_can_run_on_a_model_of_its_own():
    api = ScriptedAPI(sse_message([{"type": "text", "text": findings_json()}]))
    evaluate(api, s=settings(analysis_model="claude-opus-5", evaluator_model="claude-haiku-4-5"))
    assert api.requests[0]["model"] == "claude-haiku-4-5"


def test_an_unset_evaluator_model_is_the_analysts():
    assert settings(analysis_model="claude-opus-5", evaluator_model="").evaluator_model == (
        "claude-opus-5"
    )


# ==========================================================================
# Deterministic checks on what came back
# ==========================================================================


def test_findings_pointing_at_a_quote_that_does_not_exist_are_not_findings():
    errors = EV.validate_findings(
        EvaluatorFindings.model_validate_json(
            findings_json(quote_support=[{"quote_index": 7, "sub_requirement_id": "rotation",
                                          "support": "supports", "note": ""}])
        ),
        request(),
    )
    assert [e.code for e in errors] == ["out_of_range"]


def test_findings_naming_a_sub_requirement_this_criterion_does_not_have():
    errors = EV.validate_findings(
        EvaluatorFindings.model_validate_json(findings_json(missing_searches=["mfa"])), request()
    )
    assert [e.code for e in errors] == ["unknown_sub_requirement"]


def test_a_confidence_outside_zero_to_one_is_a_structural_error():
    errors = EV.validate_findings(
        EvaluatorFindings.model_validate_json(findings_json(critic_confidence=1.4)), request()
    )
    assert [e.code for e in errors] == ["range"]


def test_judging_one_sub_requirement_twice_is_a_structural_error():
    errors = EV.validate_findings(
        EvaluatorFindings.model_validate_json(findings_json(status_agreement=[
            {"sub_requirement_id": "rotation", "agreement": "agree", "note": ""},
            {"sub_requirement_id": "rotation", "agreement": "too_strong", "note": ""},
        ])),
        request(),
    )
    assert [e.code for e in errors] == ["duplicate"]


def test_the_critic_may_add_to_the_e1_facts_but_never_remove_one():
    """`vaulting` is unsearched in the log. The critic reports `rotation` and
    stays silent about `vaulting`; both reach the Router."""
    api = ScriptedAPI(
        sse_message([{"type": "text", "text": findings_json(missing_searches=["rotation"])}])
    )
    assert evaluate(api).findings.missing_searches == ["rotation", "vaulting"]


# ==========================================================================
# The ladder
# ==========================================================================


def test_a_truncated_answer_is_retried_and_the_wait_backs_off():
    api = ScriptedAPI(
        sse_message([{"type": "text", "text": '{"quote_su'}], stop_reason="max_tokens"),
        sse_message([{"type": "text", "text": findings_json()}]),
    )
    sleeps = []
    evaluation = evaluate(api, sleeps=sleeps)
    assert evaluation.attempts == 2 and api.calls == 2
    assert len(sleeps) == 1 and 0 < sleeps[0] <= EV.CRITIC_BACKOFF_BASE


def test_findings_that_fail_the_deterministic_checks_count_as_a_failed_attempt():
    api = ScriptedAPI(
        sse_message([{"type": "text", "text": findings_json(
            quote_support=[{"quote_index": 9, "sub_requirement_id": "rotation",
                            "support": "supports", "note": ""}])}]),
        sse_message([{"type": "text", "text": findings_json()}]),
    )
    assert evaluate(api).attempts == 2


def test_the_ladder_gives_up_after_three_attempts_with_growing_waits():
    api = ScriptedAPI(*[
        sse_message([{"type": "text", "text": "not json at all"}])
        for _ in range(EV.CRITIC_ATTEMPTS)
    ])
    sleeps = []
    with pytest.raises(EV.EvaluationFailed) as exc:
        evaluate(api, sleeps=sleeps)
    assert api.calls == EV.CRITIC_ATTEMPTS
    assert len(sleeps) == EV.CRITIC_ATTEMPTS - 1
    assert sleeps[1] > sleeps[0]  # full-jitter exponential: the ceiling doubles
    assert "c1" in str(exc.value)


def test_the_transport_ladder_is_not_run_twice():
    """`http_client.RetryingTransport` already retried this with backoff -- it is
    the process's one retry loop. Retrying it again here would be a second
    policy against the same server, which is what one-transport rules out."""
    api = ScriptedAPI(500, 500, 500, 500)
    with pytest.raises(Exception) as exc:
        EV.evaluate(request(), CRITERION, settings=settings(),
                    client=scripted_client(api, retries=3), sleep=lambda _s: None)
    assert not isinstance(exc.value, EV.EvaluationFailed)
    assert api.calls == 4  # the transport's four, and not a twelfth


def test_the_cost_of_a_failed_ladder_is_still_counted():
    """Three attempts were paid for. A criterion that ends `unevaluated` should
    not look free on the KPI page."""
    api = ScriptedAPI(*[
        sse_message([{"type": "text", "text": "nope"}], input_tokens=300, output_tokens=10)
        for _ in range(EV.CRITIC_ATTEMPTS)
    ])
    with pytest.raises(EV.EvaluationFailed):
        evaluate(api)
    assert api.calls == EV.CRITIC_ATTEMPTS


def test_a_successful_evaluation_reports_what_it_cost():
    api = ScriptedAPI(
        sse_message([{"type": "text", "text": findings_json()}],
                    input_tokens=1200, output_tokens=200)
    )
    evaluation = evaluate(api)
    assert evaluation.usage.input_tokens == 1200
    assert evaluation.cost_usd > 0
    assert evaluation.model == "claude-sonnet-5"
