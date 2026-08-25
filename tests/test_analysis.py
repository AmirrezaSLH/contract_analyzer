"""Analysis: the validator's rules, the correction rounds, and the confidence.

The validator is exercised rule by rule on synthetic drafts over a synthetic
ledger, red and green. The finisher is exercised end to end through the
scripted API: the assertion that matters is that a correction turn names the
malformed path and says what is wrong with it -- and says nothing about what
the state or the quote should be.
"""

from __future__ import annotations

import json

import pytest

from conftest import ScriptedAPI, make_chunk, scripted_client, sse_message
from contract_analyzer.compliance import (
    ComplianceDraft,
    Criterion,
    SubRequirement,
    get_criteria,
    get_criterion,
    normalize_quote,
    validate_structure,
)
from contract_analyzer.compliance.validate import derived_state
from contract_analyzer.config import Settings
from contract_analyzer.generation import analysis as AN
from contract_analyzer.generation import tools as T
from contract_analyzer.generation.tools import Evidence

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


def ledger() -> Evidence:
    ev = Evidence()
    ev.register([make_chunk(1, ROTATION), make_chunk(2, DEFAULTS, page="10")])
    return ev


def draft(**overrides) -> ComplianceDraft:
    fields = dict(
        compliance_question=CRITERION.question,
        compliance_state="Fully Compliant",
        sub_requirements=[
            {"id": "rotation", "requirement": "Rotation", "status": "met", "quote_indexes": [0]},
            {"id": "no_default", "requirement": "Defaults", "status": "met", "quote_indexes": [1]},
        ],
        relevant_quotes=[
            {"text": "rotated at least every ninety (90) days", "evidence_id": "E1"},
            {"text": 'prohibit "default" passwords', "evidence_id": "E2"},
        ],
        rationale="Both clauses are explicit.",
        raw_confidence=0.9,
    )
    fields.update(overrides)
    return ComplianceDraft.model_validate(fields)


def codes(d: ComplianceDraft) -> list[str]:
    return sorted(e.code for e in validate_structure(d, ledger(), CRITERION))


# --------------------------------------------------------------------------
# Validator rules
# --------------------------------------------------------------------------


def test_a_well_formed_draft_has_no_errors():
    assert validate_structure(draft(), ledger(), CRITERION) == []


def test_quote_matching_folds_case_quotes_dashes_and_whitespace():
    assert normalize_quote("“Default”  Pass­words — now") == normalize_quote(
        '"default" pass­words - now'
    )
    d = draft(relevant_quotes=[
        {"text": "ROTATED   at least every ninety (90) days", "evidence_id": "E1"},
        {"text": "prohibit “default” passwords", "evidence_id": "E2"},
    ])
    assert codes(d) == []


def test_a_table_row_quoted_as_prose_is_verbatim():
    """A model quoting a requirement row drops the pipes and the padding."""
    grid = ("|ID|Requirement|Frequency|\n|---|---|---|\n"
            "| GOV-01 | Security governance program | Annually |")
    ev = Evidence()
    ev.register([make_chunk(1, ROTATION),
                 make_chunk(2, "ignored", section="Exhibit G", element_type="table",
                            payload=grid)])
    d = draft(relevant_quotes=[
        {"text": "rotated at least every ninety (90) days", "evidence_id": "E1"},
        {"text": "GOV-01 Security governance program Annually", "evidence_id": "E2"},
    ])
    assert validate_structure(d, ev, CRITERION) == []
    assert normalize_quote("| GOV-01 | Security governance program |") == normalize_quote(
        "GOV-01 Security governance program"
    )


def test_a_changed_word_is_not_verbatim():
    d = draft(relevant_quotes=[
        {"text": "rotated at least every sixty (60) days", "evidence_id": "E1"},
        {"text": 'prohibit "default" passwords', "evidence_id": "E2"},
    ])
    errors = validate_structure(d, ledger(), CRITERION)
    assert [(e.path, e.code) for e in errors] == [("relevant_quotes[0].text", "not_verbatim")]
    assert "copy the exact text" in errors[0].message


def test_an_unknown_evidence_id_lists_the_known_ones():
    d = draft(relevant_quotes=[
        {"text": "rotated", "evidence_id": "E9"},
        {"text": 'prohibit "default" passwords', "evidence_id": "E2"},
    ])
    errors = validate_structure(d, ledger(), CRITERION)
    assert errors[0].code == "unknown_evidence" and "E1, E2" in errors[0].message


def test_a_paraphrased_question_is_a_structural_error():
    d = draft(compliance_question="Does the contract mandate rotating passwords?")
    assert "not_verbatim" in codes(d)
    assert any(e.path == "compliance_question" for e in validate_structure(d, ledger(), CRITERION))


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["met", "met"], "Fully Compliant"),
        (["met", "partial"], "Partially Compliant"),
        (["partial", "missing"], "Partially Compliant"),
        (["met", "not_determined"], "Partially Compliant"),
        (["missing", "not_determined"], "Non-Compliant"),
        (["missing", "missing"], "Non-Compliant"),
    ],
)
def test_the_state_is_derived_from_the_statuses(statuses, expected):
    assert derived_state(statuses) == expected


def test_a_state_that_does_not_follow_from_the_statuses_is_inconsistent():
    d = draft(compliance_state="Partially Compliant")
    assert codes(d) == ["inconsistent"]


def test_met_needs_a_quote_and_missing_must_not_have_one():
    subs = [
        {"id": "rotation", "requirement": "R", "status": "met", "quote_indexes": []},
        {"id": "no_default", "requirement": "D", "status": "missing", "quote_indexes": [1]},
    ]
    d = draft(sub_requirements=subs, compliance_state="Partially Compliant")
    assert codes(d) == ["needs_quote", "unexpected_quote"]


def test_quote_indexes_must_point_into_relevant_quotes():
    subs = [
        {"id": "rotation", "requirement": "R", "status": "met", "quote_indexes": [0, 5]},
        {"id": "no_default", "requirement": "D", "status": "met", "quote_indexes": [1]},
    ]
    assert codes(draft(sub_requirements=subs)) == ["out_of_range"]


def test_sub_requirement_ids_must_match_the_criterion_exactly():
    subs = [
        {"id": "rotation", "requirement": "R", "status": "met", "quote_indexes": [0]},
        {"id": "rotation", "requirement": "R", "status": "met", "quote_indexes": [0]},
    ]
    errors = validate_structure(draft(sub_requirements=subs), ledger(), CRITERION)
    assert [e.code for e in errors] == ["ids"] and "rotation, no_default" in errors[0].message


def test_duplicate_long_and_empty_quotes():
    d = draft(relevant_quotes=[
        {"text": "rotated at least every ninety (90) days", "evidence_id": "E1"},
        {"text": "Rotated at least every ninety (90) days", "evidence_id": "E1"},
        {"text": "", "evidence_id": "E2"},
        {"text": "x" * 301, "evidence_id": "E2"},
    ])
    assert codes(d) == ["duplicate", "empty", "not_verbatim", "not_verbatim", "too_long"]


def test_rationale_and_confidence_sanity():
    assert codes(draft(rationale="  ")) == ["empty"]
    assert codes(draft(raw_confidence=1.5)) == ["range"]


def test_the_shipped_criteria_load_with_unique_sub_requirement_ids():
    criteria = get_criteria()
    assert [c.id for c in criteria] == [
        "password_management", "it_asset_management", "training_background_checks",
        "data_in_transit", "network_auth",
    ]
    assert len(get_criterion("password_management").sub_requirements) == 7
    assert "MFA" in get_criterion("network_auth").sub_requirements_text()
    with pytest.raises(KeyError, match="known: password_management"):
        get_criterion("nope")


def test_the_draft_schema_is_a_closed_object_with_every_field_required():
    schema = ComplianceDraft.output_format()["schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"])
    assert schema["$defs"]["Quote"]["additionalProperties"] is False


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (dict(verified=2, claimed=2, not_determined=0, total=4), 0.9),
        (dict(verified=1, claimed=2, not_determined=0, total=4), 0.45),
        (dict(verified=2, claimed=2, not_determined=1, total=4), 0.675),
        (dict(verified=0, claimed=0, not_determined=0, total=4), 0.9),  # no quotes claimed
        (dict(verified=0, claimed=2, not_determined=0, total=4), 0.05),  # floor
    ],
)
def test_confidence_formula(kw, expected):
    value, components = AN.compute_confidence(0.9, needs_review=False, ended_by="model", **kw)
    assert value == pytest.approx(expected)
    assert components["cap"] == 1.0


def test_an_omitted_sub_requirement_counts_as_unsettled():
    """Found by a live run: a degenerate redraft kept one sub-requirement of
    seven and reported `coverage: 1.0`, because only statuses *present* were
    counted. A sub-requirement the draft never mentioned is not settled."""
    from contract_analyzer.compliance.schemas import SubRequirementResult

    only_rotation = [SubRequirementResult(
        id="rotation", requirement="Rotation", status="met", quote_indexes=[0]
    )]
    assert AN.undetermined_count(only_rotation, CRITERION) == 1  # no_default is absent

    with_not_determined = only_rotation + [SubRequirementResult(
        id="no_default", requirement="Defaults", status="not_determined", quote_indexes=[]
    )]
    assert AN.undetermined_count(with_not_determined, CRITERION) == 1
    assert AN.undetermined_count([], CRITERION) == 2


def test_confidence_is_capped_on_review_or_a_capped_run_and_never_reaches_one():
    full = dict(verified=2, claimed=2, not_determined=0, total=2)
    assert AN.compute_confidence(1.0, needs_review=False, ended_by="model", **full)[0] == 0.95
    assert AN.compute_confidence(0.9, needs_review=True, ended_by="model", **full)[0] == 0.5
    value, components = AN.compute_confidence(0.9, needs_review=False, ended_by="cap", **full)
    assert value == 0.5 and components["cap"] == 0.5
    assert AN.compute_confidence(0.3, needs_review=True, ended_by="cap", **full)[0] == 0.3


# --------------------------------------------------------------------------
# The finisher, end to end over the scripted API
# --------------------------------------------------------------------------


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="k", log_file=None, embedding_provider="fake",
                structure_fix_rounds=2, analysis_max_tool_calls=4, analysis_effort="medium")
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def searches(monkeypatch):
    """`search_contract` returns the two ledger chunks; nothing else is retrieved."""
    from contract_analyzer.retrieval.base import RetrievalResult

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=[make_chunk(1, ROTATION), make_chunk(2, DEFAULTS, page="10")],
                               candidates=20, top_k=top_k or 6)

    monkeypatch.setattr(T, "retrieve", retrieve)


def draft_json(**overrides) -> str:
    return draft(**overrides).model_dump_json()


def loop_prefix():
    return [
        sse_message([{"type": "tool_use", "id": "toolu_1", "name": "search_contract",
                      "input": {"query": "password rotation", "mode": "keyword"}}],
                    stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "I have what I need."}]),
    ]


def analyze(api: ScriptedAPI, s: Settings | None = None, events=None):
    """The Analyzer's result. `analyze_criterion` returns the whole run now --
    the ledger and conversation the Router needs -- so these tests, which are
    about the draft and the correction rounds, take `.result` off it."""
    return outcome(api, s, events).result


def outcome(api: ScriptedAPI, s: Settings | None = None, events=None):
    s = s or settings()
    return AN.analyze_criterion(
        CRITERION, None, object(), s, document_id=3, client=scripted_client(api),
        on_event=events.append if events is not None else None,
    )


def test_a_clean_draft_needs_no_correction_and_the_finisher_request_is_structured(searches):
    api = ScriptedAPI(*loop_prefix(), sse_message([{"type": "text", "text": draft_json()}],
                                                  input_tokens=500, output_tokens=80))
    events = []
    result = analyze(api, events=events)

    assert api.calls == 3 and result.structure_rounds == 0 and not result.needs_review
    assert result.compliance_state == "Fully Compliant" and result.confidence == 0.9
    assert [q.verified for q in result.relevant_quotes] == [True, True]
    assert result.relevant_quotes[0].section_ref == "6.6 Password Management Standard"
    assert [q.page_display for q in result.relevant_quotes] == ["9", "10"]
    assert result.tool_calls == 1 and result.ended_by == "model"
    assert result.usage["input_tokens"] == 700 and result.cost_usd > 0
    # The Analyzer does not announce a verdict. The layer that owns the whole
    # criterion timeline does -- today the harness, and the Router once it sits
    # in front -- because a result event carrying the verdict now and the
    # duration later would be two events for one fact. `test_report.py` asserts
    # the event still reaches a caller.
    assert [e["type"] for e in events] == ["tool_call", "text"]

    finisher = api.requests[2]
    assert finisher["output_config"]["format"]["type"] == "json_schema"
    assert finisher["output_config"]["effort"] == "medium"
    # The definitions stay (same prefix as the loop, tool blocks never orphaned)
    # but no further call is wanted.
    assert [t["name"] for t in finisher["tools"]] == ["search_contract", "get_section"]
    assert finisher["tool_choice"] == {"type": "none"}
    assert "citations" not in json.dumps(finisher)
    assert "thinking" not in finisher
    # The ledger is not re-sent: the last user turn is the finish instruction, text only.
    assert isinstance(finisher["messages"][-1]["content"], str)
    assert "sixty" not in json.dumps(finisher)
    # The loop's requests carried the criterion and its sub-requirements.
    assert "rotation: Rotation at least every 90 days" in api.requests[0]["system"]
    assert result.model == "claude-sonnet-5"
    assert all(req["model"] == result.model for req in api.requests)


def test_analysis_uses_analysis_model_not_the_chat_model(searches):
    """Chat and analysis are independently tunable: an experiment can cheapen
    one surface without moving the other."""
    s = settings(answer_model="claude-opus-5", analysis_model="claude-sonnet-5")
    api = ScriptedAPI(*loop_prefix(), sse_message([{"type": "text", "text": draft_json()}]))
    result = analyze(api, s)
    assert result.model == "claude-sonnet-5"
    assert {req["model"] for req in api.requests} == {"claude-sonnet-5"}


def test_a_non_verbatim_quote_triggers_exactly_one_correction_turn(searches):
    bad = draft_json(relevant_quotes=[
        {"text": "rotated at least every sixty (60) days", "evidence_id": "E1"},
        {"text": 'prohibit "default" passwords', "evidence_id": "E2"},
    ])
    api = ScriptedAPI(*loop_prefix(),
                      sse_message([{"type": "text", "text": bad}]),
                      sse_message([{"type": "text", "text": draft_json()}]))
    result = analyze(api)

    assert api.calls == 4 and result.structure_rounds == 1 and not result.needs_review
    assert all(q.verified for q in result.relevant_quotes) and result.confidence == 0.9
    correction = api.requests[3]["messages"][-1]
    assert correction["role"] == "user"
    text = correction["content"]
    assert "`relevant_quotes[0].text`" in text and "not verbatim in E1" in text
    # What is malformed, never what the answer should be.
    assert "ninety" not in text and "Fully" not in text and "Non-Compliant" not in text
    # The rejected draft went back as the assistant turn before it.
    assert api.requests[3]["messages"][-2]["role"] == "assistant"
    assert api.requests[3]["output_config"]["format"]["type"] == "json_schema"
    assert api.requests[3]["tool_choice"] == {"type": "none"} and "tools" in api.requests[3]


def test_errors_that_survive_the_rounds_drop_the_quote_and_flag_review(searches):
    bad = draft_json(relevant_quotes=[
        {"text": "rotated at least every sixty (60) days", "evidence_id": "E1"},
        {"text": 'prohibit "default" passwords', "evidence_id": "E2"},
    ])
    api = ScriptedAPI(*loop_prefix(), *[sse_message([{"type": "text", "text": bad}])] * 3)
    result = analyze(api)

    assert api.calls == 5 and result.structure_rounds == 2 and result.needs_review
    assert [q.evidence_id for q in result.relevant_quotes] == ["E2"]
    assert all(q.verified for q in result.relevant_quotes)
    # Indexes were remapped after the drop: no_default still points at its quote.
    assert [s.quote_indexes for s in result.sub_requirements] == [[], [0]]
    assert result.confidence <= 0.5 and result.confidence_bucket == "Low"
    assert result.unresolved_errors == [
        "`relevant_quotes[0].text`: not verbatim in E1 -- copy the exact text"
    ]


def test_zero_rounds_means_no_correction_turn(searches):
    bad = draft_json(rationale="")
    api = ScriptedAPI(*loop_prefix(), sse_message([{"type": "text", "text": bad}]))
    result = analyze(api, settings(structure_fix_rounds=0))
    assert api.calls == 3 and result.needs_review and result.structure_rounds == 0


def test_a_truncated_draft_is_retried_once_then_fails(searches):
    api = ScriptedAPI(*loop_prefix(),
                      sse_message([{"type": "text", "text": "{"}], stop_reason="max_tokens"),
                      sse_message([{"type": "text", "text": draft_json()}]))
    assert analyze(api).structure_rounds == 0 and api.calls == 4

    api = ScriptedAPI(*loop_prefix(),
                      sse_message([{"type": "text", "text": ""}], stop_reason="refusal"),
                      sse_message([{"type": "text", "text": ""}], stop_reason="refusal"))
    with pytest.raises(AN.AnalysisFailed, match="refusal"):
        analyze(api)


def test_a_capped_run_still_finishes_and_caps_confidence(searches):
    search = sse_message([{"type": "tool_use", "id": "toolu_x", "name": "search_contract",
                           "input": {"query": "x"}}], stop_reason="tool_use")
    api = ScriptedAPI(search, sse_message([{"type": "text", "text": draft_json()}]))
    result = analyze(api, settings(analysis_max_tool_calls=1))
    assert result.ended_by == "cap" and result.confidence == 0.5 and not result.needs_review
    assert result.confidence_components["cap"] == 0.5
