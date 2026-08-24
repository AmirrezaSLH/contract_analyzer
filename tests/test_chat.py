"""Chat: the finisher's request shape, the citations, no-context, history.

The scripted API returns `citations_delta` events the way the API does, so
the resolution from `document_index` back to a ledger entry is checked
against what the SDK assembled, not against a dict the test wrote.
"""

from __future__ import annotations

import json

import pytest

from conftest import ScriptedAPI, make_chunk, scripted_client, sse_message
from contract_analyzer.config import Settings
from contract_analyzer.generation import blocks as B
from contract_analyzer.generation import tools as T
from contract_analyzer.generation.chat import chat, replay_history
from contract_analyzer.generation.prompts import get_prompts
from contract_analyzer.generation.tools import Evidence
from contract_analyzer.retrieval.base import RetrievalResult, RetrievedChunk

MFA = "Supplier shall enforce multi-factor authentication for all privileged accounts."
SSO = "Users authenticate through SAML 2.0 single sign-on."


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="k", log_file=None, embedding_provider="fake",
                chat_effort="low", chat_max_tool_calls=4)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def searches(monkeypatch):
    """`search_contract` returns two chunks, or nothing when the query says so."""

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        chunks = [] if "nothing" in question else [
            make_chunk(1, MFA, section="6.2 Authentication", page="4"),
            make_chunk(2, SSO, section="6.2 Authentication", page="4"),
        ]
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=chunks, candidates=20, top_k=top_k or 6)

    monkeypatch.setattr(T, "retrieve", retrieve)


def search_turn(query="MFA privileged"):
    return sse_message([{"type": "tool_use", "id": "toolu_s", "name": "search_contract",
                         "input": {"query": query, "mode": "hybrid"}}], stop_reason="tool_use")


def done_turn():
    return sse_message([{"type": "text", "text": "Found it."}])


def citation(index: int, text: str, start: int, end: int) -> dict:
    return {"type": "char_location", "cited_text": text, "document_index": index,
            "document_title": "t", "start_char_index": start, "end_char_index": end}


def ask(api, question="Does the vendor have to use MFA?", *, history=(), on_text=None, s=None):
    return chat(question, None, object(), s or settings(), document_id=5, history=history,
                  client=scripted_client(api), on_text=on_text)


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def test_document_blocks_one_per_entry_in_e_order_with_citations_on():
    ev = Evidence()
    ev.register([make_chunk(1, MFA, section="6.2 Authentication", page="4"),
                 make_chunk(2, SSO, section="6.2 Authentication", page="4")])
    blocks = B.document_blocks(ev)
    assert [b["source"]["data"] for b in blocks] == [MFA, SSO]
    for block in blocks:
        assert block["type"] == "document" and block["citations"] == {"enabled": True}
        assert block["source"] == {"type": "text", "media_type": "text/plain",
                                   "data": block["source"]["data"]}
        assert block["title"] == "6. Identity and Access Management > 6.2 Authentication (p.4)"
        assert block["context"] == "Sample Contract.pdf, paragraph"


def test_a_block_flags_inferred_sections_and_shows_a_table_with_its_breadcrumb():
    chunk = RetrievedChunk(
        chunk_id=3, document_id=1, ordinal=3, content="x", filename="c.pdf", page=9,
        page_label="9", page_end=10, page_label_end="10", section="Exhibit G",
        section_path=["Exhibit G"], element_type="table", payload="| a | b |",
        spine_source="headings",
    )
    ev = Evidence()
    ev.register([chunk])
    block = B.document_block(next(iter(ev)))
    assert block["context"] == "c.pdf, table, sections inferred"
    assert block["title"] == "Exhibit G (p.9-10)"
    assert block["source"]["data"] == "Exhibit G\n| a | b |"


# --------------------------------------------------------------------------
# The finisher
# --------------------------------------------------------------------------


def test_the_finisher_request_carries_the_blocks_then_the_question_and_nothing_else(searches):
    api = ScriptedAPI(search_turn(), done_turn(),
                      sse_message([{"type": "text", "text": "Yes."}]))
    ask(api)
    assert api.calls == 3
    final = api.requests[2]
    content = final["messages"][-1]["content"]
    assert [c["type"] for c in content] == ["document", "document", "text"]
    assert content[-1]["text"] == "Does the vendor have to use MFA?"
    assert [c["source"]["data"] for c in content[:2]] == [MFA, SSO]
    assert all(c["citations"] == {"enabled": True} for c in content[:2])
    assert "tools" not in final and "format" not in final["output_config"]
    assert final["output_config"] == {"effort": "low"} and "thinking" not in final
    assert final["stream"] is True
    # The loop's tool traffic is not in the finisher's conversation.
    assert "tool_result" not in json.dumps(final["messages"])
    # The loop's own requests, for their part, ran at the chat effort with the tools.
    assert api.requests[0]["output_config"] == {"effort": "low"}
    assert [t["name"] for t in api.requests[0]["tools"]] == ["search_contract", "get_section"]


def test_citations_resolve_to_the_right_passage_and_are_verbatim(searches):
    quote = "multi-factor authentication for all privileged accounts"
    start = MFA.index(quote)
    answer = sse_message([
        {"type": "text", "text": "Yes. The contract requires "},
        {"type": "text", "text": quote,
         "citations": [citation(0, quote, start, start + len(quote))]},
        {"type": "text", "text": " and SSO for users.",
         "citations": [citation(1, "SAML 2.0", 26, 34), citation(7, "ghost", 0, 5)]},
    ])
    api = ScriptedAPI(search_turn(), done_turn(), answer)
    result = ask(api)

    assert result.text == f"Yes. The contract requires {quote} and SSO for users."
    assert [c.evidence_id for c in result.citations] == ["E1", "E2"]  # out-of-range 7 dropped
    first = result.citations[0]
    assert first.quote == quote and MFA[first.start:first.end] == quote
    assert first.page_display == "4" and first.title.endswith("6.2 Authentication (p.4)")
    assert result.citations[1].entry.chunk.content == SSO
    assert result.grounded and result.stop_reason == "end_turn" and result.ended_by == "model"
    assert len(result.evidence) == 2 and len(result.tool_calls) == 1


def test_an_answer_without_citations_is_not_an_error(searches):
    api = ScriptedAPI(search_turn(), done_turn(),
                      sse_message([{"type": "text", "text": "Not stated."}]))
    result = ask(api)
    assert result.text == "Not stated." and result.citations == []


def test_an_empty_ledger_answers_no_context_without_a_finisher_call(searches):
    api = ScriptedAPI(search_turn("nothing about this"), done_turn())
    seen = []
    result = ask(api, "What is the vendor's favourite colour?", on_text=seen.append)
    assert api.calls == 2  # the loop's two; the finisher never touched the transport
    assert result.text == get_prompts(settings()).get("chat.no_context")
    assert seen == [result.text]
    assert not result.grounded and result.stop_reason == "no_context"
    assert result.citations == [] and len(result.evidence) == 0


def test_on_text_deltas_concatenate_to_the_answer(searches):
    text = "The vendor must enforce MFA for every privileged account, per Section 6.2 (p.4)."
    api = ScriptedAPI(search_turn(), done_turn(), sse_message([{"type": "text", "text": text}]))
    deltas = []
    result = ask(api, on_text=deltas.append)
    assert len(deltas) > 1 and "".join(deltas) == text == result.text


def test_history_is_replayed_as_text_only_and_capped(searches):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
               for i in range(12)]
    api = ScriptedAPI(search_turn(), done_turn(),
                      sse_message([{"type": "text", "text": "ok"}]))
    ask(api, "for which accounts?", history=history)
    for request in api.requests:
        messages = request["messages"]
        replayed = [m for m in messages if isinstance(m["content"], str)
                    and m["content"].startswith("turn ")]
        assert [m["content"] for m in replayed] == [f"turn {i}" for i in range(4, 12)]
        assert replayed[0]["role"] == "user"
    # The loop saw the question after the history; the finisher saw the blocks after it.
    assert api.requests[0]["messages"][-1] == {"role": "user", "content": "for which accounts?"}
    assert api.requests[2]["messages"][-1]["content"][-1]["text"] == "for which accounts?"


def test_replay_history_drops_blank_and_foreign_roles():
    kept = replay_history([
        {"role": "user", "content": "a"}, {"role": "system", "content": "x"},
        {"role": "assistant", "content": "  "}, {"role": "assistant", "content": 42},
    ])
    assert kept == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "42"}]
