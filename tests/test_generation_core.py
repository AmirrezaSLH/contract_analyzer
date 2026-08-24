"""Generation core: client, prompts, tools, ledger and the agent loop.

Everything here drives the real `anthropic` SDK over canned SSE through the
project's own transport (`conftest.ScriptedAPI`). What is asserted is what
the SDK *sent* -- tool definitions, `output_config`, no `thinking` -- and
what the loop did with what came back, not a fake client that agrees with
the code by construction.

Retrieval is replaced by a fake that returns synthetic chunks and counts its
calls, so the tools' claims -- scope bound in Python, ledger ids stable,
dedupe answered without retrieval, caps reported as results -- are checked
without a database. One test at the end uses the two-contract corpus to
show the bound scope cannot leak.
"""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from conftest import ScriptedAPI, make_chunk, scripted_client, sse_message
from contract_analyzer.config import Settings
from contract_analyzer.generation import agent as A
from contract_analyzer.generation import tools as T
from contract_analyzer.generation.client import AnswerUnavailable, Usage, get_client
from contract_analyzer.generation.pricing import PRICES, cost_usd
from contract_analyzer.generation.prompts import PromptError, PromptLibrary, get_prompts
from contract_analyzer.http_client import HttpFailure

CHUNKS = {
    1: make_chunk(1, "Passwords shall be rotated at least every ninety (90) days."),
    2: make_chunk(2, "Default passwords are prohibited on every system.", page="10"),
    3: make_chunk(3, "Privileged credentials shall be vaulted.", section="6.7 Vaulting", page="10"),
}


class FakeRetrieval:
    """Stands in for `retrieve()` and `retrieve_by_section()`; counts and records."""

    def __init__(self, *result_sets: list[int]) -> None:
        self.result_sets = list(result_sets)
        self.calls: list[dict] = []

    def retrieve(self, question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        from contract_analyzer.retrieval.base import RetrievalResult

        self.calls.append({"question": question, "document_id": document_id, "mode": mode,
                           "top_k": top_k})
        ids = self.result_sets.pop(0) if self.result_sets else []
        chunks = [CHUNKS[i] for i in ids]
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=chunks, candidates=20, top_k=top_k or 6)

    def retrieve_by_section(self, conn, document_id, pattern, *, limit=20):
        self.calls.append({"section": pattern, "document_id": document_id, "limit": limit})
        ids = self.result_sets.pop(0) if self.result_sets else []
        return [CHUNKS[i] for i in ids]


@pytest.fixture
def fake_retrieval(monkeypatch):
    def install(*result_sets):
        fake = FakeRetrieval(*result_sets)
        monkeypatch.setattr(T, "retrieve", fake.retrieve)
        monkeypatch.setattr(T, "retrieve_by_section", fake.retrieve_by_section)
        return fake

    return install


def settings(**overrides) -> Settings:
    # Pin the model this module prices against. Settings() still reads
    # settings.json for unspecified fields, and that file is the experiment
    # knob -- a cheaper analysis_model must not move these assertions.
    base = dict(anthropic_api_key="test-key", log_file=None, embedding_provider="fake",
                answer_model="claude-opus-5")
    base.update(overrides)
    return Settings(**base)


EMBEDDER = object()  # any non-None: the fake retrieval never calls it


def tools(settings_=None, *, embedder=EMBEDDER, **kw) -> T.ContractTools:
    return T.ContractTools(conn=None, document_id=7, embedder=embedder,
                           settings=settings_ or settings(), **kw)


def tool_use(name, **args):
    return {"type": "tool_use", "id": f"toolu_{len(json.dumps(args))}_{name}", "name": name,
            "input": args}


# --------------------------------------------------------------------------
# Tool definitions and execution
# --------------------------------------------------------------------------


def test_definitions_carry_the_mode_and_top_k_bounds_and_no_document_id():
    defs = {d["name"]: d for d in tools().definitions()}
    search = defs["search_contract"]["input_schema"]
    assert search["properties"]["mode"]["enum"] == ["hybrid", "vector", "keyword"]
    assert search["properties"]["top_k"]["minimum"] == 1
    assert search["properties"]["top_k"]["maximum"] == 12
    assert search["required"] == ["query"]
    assert "document_id" not in json.dumps(defs)
    assert defs["get_section"]["input_schema"]["required"] == ["prefix"]


def test_search_reaches_retrieve_with_the_models_choices_and_the_bound_scope(fake_retrieval):
    fake = fake_retrieval([1, 2])
    t = tools()
    outcome = t.execute("search_contract", {"query": "password rotation", "mode": "keyword",
                                            "top_k": 3})
    assert fake.calls == [{"question": "password rotation", "document_id": 7, "mode": "keyword",
                           "top_k": 3}]
    assert t.evidence.ids == ["E1", "E2"]
    assert outcome.text.startswith("[E1] 6. Identity and Access Management > 6.6 Password "
                                   "Management Standard (p.9)\nPasswords shall be rotated")
    assert "[E2]" in outcome.text and "(p.10)" in outcome.text
    assert outcome.call.retrieved and outcome.call.new == 2 and outcome.call.returned == 2


def test_an_overlapping_second_call_renders_the_seen_chunk_as_an_id_only(fake_retrieval):
    fake_retrieval([1, 2], [2, 3])
    t = tools()
    t.execute("search_contract", {"query": "rotation"})
    outcome = t.execute("search_contract", {"query": "vaulting", "mode": "vector"})
    assert t.evidence.ids == ["E1", "E2", "E3"]  # ids are stable and in first-seen order
    assert "[E3] " in outcome.text and "[E2]" not in outcome.text
    assert "already retrieved: E2" in outcome.text
    assert outcome.call.new == 1 and outcome.call.returned == 2 and outcome.call.ids == ["E2", "E3"]


def test_an_identical_repeated_call_does_not_hit_retrieval(fake_retrieval):
    fake = fake_retrieval([1], [3])
    t = tools()
    t.execute("search_contract", {"query": "rotation", "top_k": 2})
    repeat = t.execute("search_contract", {"top_k": 2, "query": "rotation"})  # same args, reordered
    assert len(fake.calls) == 1
    assert not repeat.call.retrieved and repeat.call.note == "duplicate"
    assert "already retrieved: E1" in repeat.text
    assert len(t.evidence) == 1


def test_get_section_wraps_retrieve_by_section(fake_retrieval):
    fake = fake_retrieval([3])
    outcome = tools().execute("get_section", {"prefix": "6.7"})
    assert fake.calls == [{"section": "6.7", "document_id": 7, "limit": T.SECTION_LIMIT}]
    assert "[E1] " in outcome.text and "6.7 Vaulting" in outcome.text


def test_vector_without_an_embedder_is_a_result_naming_keyword_not_an_exception(fake_retrieval):
    fake = fake_retrieval([1])
    t = tools(embedder=None)
    for mode in ("vector", "hybrid"):
        outcome = t.execute("search_contract", {"query": "x", "mode": mode})
        assert outcome.call.error and "keyword" in outcome.text
    assert fake.calls == []
    assert "only `keyword` works" in t.definitions()[0]["description"]
    ok = t.execute("search_contract", {"query": "x", "mode": "keyword"})
    assert ok.call.retrieved and len(fake.calls) == 1


@pytest.mark.parametrize(
    ("name", "args", "fragment"),
    [
        ("search_contract", {"query": "  "}, "must not be empty"),
        ("search_contract", {"query": "x", "mode": "fuzzy"}, "mode must be one of"),
        ("search_contract", {"query": "x", "top_k": "many"}, "top_k must be an integer"),
        ("get_section", {"prefix": ""}, "must not be empty"),
        ("open_other_contract", {"document_id": 2}, "unknown tool"),
    ],
)
def test_bad_input_is_an_error_result_never_a_raise(fake_retrieval, name, args, fragment):
    fake = fake_retrieval([1])
    outcome = tools().execute(name, args)
    assert outcome.call.error and fragment in outcome.text
    assert fake.calls == []


def test_top_k_is_clamped_to_the_bounds(fake_retrieval):
    fake = fake_retrieval([1], [2])
    t = tools()
    t.execute("search_contract", {"query": "a", "top_k": 99})
    t.execute("search_contract", {"query": "b", "top_k": 0})
    assert [c["top_k"] for c in fake.calls] == [T.MAX_TOP_K, T.MIN_TOP_K]


def test_a_full_evidence_budget_is_reported_not_retrieved(fake_retrieval):
    fake = fake_retrieval([1], [2])
    t = tools(max_evidence_tokens=5)
    first = t.execute("search_contract", {"query": "a"})
    assert t.budget_reached and "Evidence budget reached" in first.text
    second = t.execute("search_contract", {"query": "b"})
    assert len(fake.calls) == 1
    assert second.call.note == "budget" and not second.call.error
    assert "Do not search again" in second.text


def test_a_table_chunk_is_shown_with_its_breadcrumb(fake_retrieval):
    grid = "| ID | Requirement |\n|---|---|\n| PASS-01 | Rotate |"
    table = make_chunk(9, "ignored", section="Exhibit G", payload=grid, element_type="table")
    CHUNKS[9] = table
    try:
        fake_retrieval([9])
        outcome = tools().execute("get_section", {"prefix": "Exhibit G"})
    finally:
        del CHUNKS[9]
    assert "Exhibit G\n| ID |" in outcome.text


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def task(**overrides) -> A.AgentTask:
    fields = dict(surface="chat", system="sys", effort="low", max_tool_calls=4,
                  messages=[{"role": "user", "content": "Does the vendor rotate passwords?"}])
    fields.update(overrides)
    return A.AgentTask(**fields)


def run(api: ScriptedAPI, *, tools_=None, finisher=None, settings_=None, task_=None, events=None):
    s = settings_ or settings()
    return A.run_agent(
        task_ or task(),
        tools=tools_ or tools(s),
        finisher=finisher,
        settings=s,
        client=scripted_client(api),
        on_event=events.append if events is not None else None,
    )


def test_the_loop_executes_tool_calls_and_feeds_results_back(fake_retrieval):
    fake_retrieval([1, 2], [3])
    api = ScriptedAPI(
        sse_message([{"type": "text", "text": "Let me search."},
                     tool_use("search_contract", query="rotation", mode="keyword", top_k=2)],
                    stop_reason="tool_use"),
        sse_message([tool_use("get_section", prefix="6.7")], stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "Yes, every 90 days."}]),
    )
    finished = []
    events = []
    r = run(api, finisher=lambda run_: finished.append(run_) or "done", events=events)

    assert api.calls == 3 and r.turns == 3 and r.ended_by == "model" and r.result == "done"
    assert finished == [r]
    assert r.evidence.ids == ["E1", "E2", "E3"]
    assert [c.name for c in r.tool_calls] == ["search_contract", "get_section"]
    # The second request carries the assistant's tool_use and our tool_result, in order.
    second = api.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][-1]["type"] == "tool_use"
    assert second[-1]["role"] == "user"
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is False
    assert result["content"].startswith("[E1] ")
    assert [e["type"] for e in events] == ["text", "tool_call", "tool_call", "text"]
    assert events[1]["args"] == {"query": "rotation", "mode": "keyword", "top_k": 2}


def test_every_request_has_effort_and_tools_and_never_thinking(fake_retrieval):
    fake_retrieval([1])
    api = ScriptedAPI(
        sse_message([tool_use("search_contract", query="q")], stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "ok"}]),
    )
    run(api, task_=task(effort="medium"))
    for request in api.requests:
        assert "thinking" not in request
        assert request["output_config"] == {"effort": "medium"}
        assert request["model"] == "claude-opus-5" and request["stream"] is True
        assert request["system"] == "sys"
        assert [t["name"] for t in request["tools"]] == ["search_contract", "get_section"]


def test_the_loop_stops_at_max_tool_calls_with_ended_by_cap(fake_retrieval):
    fake_retrieval([1], [2], [3], [1])
    api = ScriptedAPI(*[
        sse_message([tool_use("search_contract", query=f"q{i}")], stop_reason="tool_use")
        for i in range(6)
    ])
    finished = []
    r = run(api, task_=task(max_tool_calls=2), finisher=finished.append)
    assert api.calls == 2 and len(r.tool_calls) == 2 and r.ended_by == "cap"
    assert len(finished) == 1
    # The last tool results were still appended, so the finisher's conversation is well-formed.
    last = r.messages[-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"


def test_parallel_tool_calls_count_individually_and_return_in_one_turn(fake_retrieval):
    fake_retrieval([1], [2], [3])
    api = ScriptedAPI(
        sse_message([tool_use("search_contract", query="a"), tool_use("search_contract", query="b"),
                     tool_use("get_section", prefix="6.7")], stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "never reached"}]),
    )
    r = run(api, task_=task(max_tool_calls=3))
    assert api.calls == 1 and r.ended_by == "cap" and len(r.tool_calls) == 3
    assert len(r.messages[-1]["content"]) == 3  # one user turn, three tool_result blocks


def test_the_loop_stops_at_max_evidence_tokens_with_ended_by_cap(fake_retrieval):
    fake_retrieval([1, 2], [3])
    api = ScriptedAPI(
        sse_message([tool_use("search_contract", query="a")], stop_reason="tool_use"),
        sse_message([tool_use("search_contract", query="b")], stop_reason="tool_use"),
    )
    s = settings()
    finished = []
    r = run(api, tools_=tools(s, max_evidence_tokens=10), finisher=finished.append, settings_=s)
    assert api.calls == 1 and r.ended_by == "cap" and len(finished) == 1
    assert "Evidence budget reached" in r.messages[-1]["content"][0]["content"]


def test_a_non_tool_stop_reason_is_recorded_and_ends_the_loop():
    api = ScriptedAPI(sse_message([{"type": "text", "text": "partial"}], stop_reason="max_tokens"))
    r = run(api)
    assert r.ended_by == "max_tokens" and api.calls == 1


def test_usage_sums_across_turns_and_cost_matches_the_pricing_table(fake_retrieval):
    fake_retrieval([1])
    api = ScriptedAPI(
        sse_message([tool_use("search_contract", query="q")], stop_reason="tool_use",
                    input_tokens=1000, output_tokens=50),
        sse_message([{"type": "text", "text": "ok"}], input_tokens=2000, output_tokens=150),
    )
    r = run(api)
    assert r.usage == Usage(input_tokens=3000, output_tokens=200)
    rate_in, rate_out = PRICES["claude-opus-5"]
    assert r.cost_usd == pytest.approx((3000 * rate_in + 200 * rate_out) / 1e6)


def test_thinking_blocks_go_back_unchanged():
    """The signature is what lets the model continue its reasoning next turn."""
    import types

    block = types.SimpleNamespace(
        model_dump=lambda **kw: {"type": "thinking", "thinking": "", "signature": "sig"}
    )
    message = types.SimpleNamespace(content=[block])
    assert A.content_params(message) == [{"type": "thinking", "thinking": "", "signature": "sig"}]


# --------------------------------------------------------------------------
# Client, errors, pricing, prompts
# --------------------------------------------------------------------------


def test_a_missing_key_raises_before_any_request():
    with pytest.raises(AnswerUnavailable, match="ANTHROPIC_API_KEY"):
        get_client(settings(anthropic_api_key=None))


def test_a_401_becomes_answer_unavailable_naming_env():
    api = ScriptedAPI(401)
    with pytest.raises(AnswerUnavailable, match=r"\.env"):
        run(api)
    assert api.calls == 1


def test_an_exhausted_transport_reaches_the_caller_as_http_failure():
    api = ScriptedAPI(*[httpx.ConnectError("refused")] * 3)
    s = settings()
    client = scripted_client(api, retries=2)
    with pytest.raises(HttpFailure, match="failed after 3 attempt"):
        A.run_agent(task(), tools=tools(s), settings=s, client=client)
    assert api.calls == 3


def test_a_400_propagates_untouched():
    import anthropic

    api = ScriptedAPI(400)
    with pytest.raises(anthropic.BadRequestError):
        run(api)


def test_cost_usd_prices_cache_tokens_and_unknown_models():
    assert cost_usd("claude-opus-5", 1_000_000, 0) == 5.0
    assert cost_usd("claude-opus-5", 0, 1_000_000) == 25.0
    assert cost_usd("claude-opus-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.5)
    assert cost_usd("claude-opus-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(6.25)
    assert cost_usd("claude-future-9", 1_000_000, 1_000_000) == 0.0


def test_the_shipped_prompt_library_loads_and_formats():
    lib = get_prompts(settings())
    assert {"agent.system", "chat.system", "chat.no_context", "analysis.system",
            "analysis.finish", "analysis.fix_structure"} <= set(lib.keys())
    text = lib.format("analysis.system", requirement="R", question="Q", sub_requirements="- s1")
    assert "Requirement: R" in text and "- s1" in text
    with pytest.raises(PromptError, match="needs a value for 'errors'"):
        lib.format("analysis.fix_structure")


def test_a_prompt_file_missing_a_key_fails_on_load_naming_the_file(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 1, "prompts": {"agent.system": "x"}}))
    with pytest.raises(PromptError) as info:
        PromptLibrary.load(path)
    assert str(path) in str(info.value)
    assert "chat.system" in str(info.value) and "it has agent.system" in str(info.value)


def test_a_prompt_file_with_the_wrong_version_or_shape_is_rejected(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 2, "prompts": {}}))
    with pytest.raises(PromptError, match="version"):
        PromptLibrary.load(path)
    with pytest.raises(PromptError, match="not found"):
        PromptLibrary.load(tmp_path / "missing.json")


def test_get_names_the_missing_prompt_and_the_ones_it_has():
    lib = PromptLibrary({"a": "1", "b": "2"})
    with pytest.raises(PromptError, match="no prompt named 'c'.*a, b"):
        lib.get("c")


# --------------------------------------------------------------------------
# Scope, on the real corpus
# --------------------------------------------------------------------------


def test_no_tool_call_can_reach_a_second_contract(ingested_sample):
    """The decoy holds the nearest vectors to a password question; bound scope must not see it."""
    corpus = ingested_sample
    t = T.ContractTools(corpus.conn, document_id=corpus.sample_id, embedder=corpus.embedder,
                        settings=corpus.settings)
    for args in (
        {"query": "password rotation break-glass credentials", "mode": "keyword", "top_k": 12},
        {"query": "password rotation break-glass credentials", "mode": "hybrid", "top_k": 12},
        {"query": corpus.decoy_token, "mode": "keyword"},
    ):
        t.execute("search_contract", args)
    t.execute("get_section", {"prefix": "3"})
    for entry in t.evidence:
        assert entry.chunk.document_id == corpus.sample_id
        assert corpus.decoy_token not in entry.chunk.content
