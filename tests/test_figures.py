"""Retrieved figures reach the model as pictures, and never break a run.

The encoder is shared with the ingest-time describer, which stays off; what
is asserted here is the *query-time* half: a figure chunk coming back from a
tool puts an image block on the tool result, the chat finisher appends the
same images beside its (still plain-text) document blocks, and every way an
asset can be unavailable degrades to caption-only rather than to an
exception.

The PNG is written to disk by the test rather than fixtured: what the code
does with a real file is the point, and a two-by-two square is 70 bytes.
"""

from __future__ import annotations

import base64
import json

import pytest

from conftest import ScriptedAPI, make_chunk, scripted_client, sse_message
from contract_analyzer.config import PROJECT_ROOT, Settings
from contract_analyzer.generation import figures as F
from contract_analyzer.generation import tools as T
from contract_analyzer.generation.chat import chat
from contract_analyzer.retrieval.base import RetrievalResult

#: A 2x2 PNG, small enough to inline and real enough for Pillow to open.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8DwnwEJMKEL"
    "0F8AAOwwAxeIrTgVAAAAAElFTkSuQmCC"
)

CAPTION = "Figure 3.1: data flows between the Supplier and the Sub-processor."
PROSE = "Personal data leaves the EEA only through the approved gateway."


def settings(**kw) -> Settings:
    base = dict(anthropic_api_key="k", log_file=None, embedding_provider="fake",
                chat_effort="low", chat_max_tool_calls=4)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def asset(tmp_path):
    """A readable PNG, addressed the way a chunk addresses it: relative to the root."""
    path = tmp_path / "figure-3-1.png"
    path.write_bytes(TINY_PNG)
    # `asset_path` is stored relative to PROJECT_ROOT; tmp_path is not under
    # it, so the relative form is the one that walks back out.
    import os

    return os.path.relpath(path, PROJECT_ROOT)


def figure_chunk(asset_path, *, chunk_id=1, panels=None):
    payload = json.dumps({"panels": panels, "description": None}) if panels else None
    return make_chunk(chunk_id, f"{CAPTION}\n{PROSE}", section="3. Data Protection",
                      page="7", element_type="figure", asset_path=asset_path, payload=payload)


@pytest.fixture
def searches(monkeypatch):
    """Install a `search_contract` that returns exactly these chunks."""

    def install(*chunks):
        def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                     top_k=None, candidates=None):
            return RetrievalResult(question=question, mode=mode or "hybrid",
                                   document_id=document_id, chunks=list(chunks), candidates=20,
                                   top_k=top_k or 6)

        monkeypatch.setattr(T, "retrieve", retrieve)

    return install


def tools(**kw):
    return T.ContractTools(None, document_id=5, settings=settings(**kw), embedder=object())


def search_turn():
    return sse_message([{"type": "tool_use", "id": "toolu_s", "name": "search_contract",
                         "input": {"query": "cross-border data flow", "mode": "hybrid"}}],
                       stop_reason="tool_use")


def ask(api, question="Where does personal data go?", **kw):
    return chat(question, None, object(), settings(**kw), document_id=5,
                client=scripted_client(api))


# --------------------------------------------------------------------------
# Resolving the panels
# --------------------------------------------------------------------------


def test_panels_come_from_the_payload_and_fall_back_to_asset_path(asset):
    chunk = figure_chunk(asset, panels=["data/assets/a.png", "data/assets/b.png"])
    assert F.panel_paths(chunk) == [PROJECT_ROOT / "data/assets/a.png",
                                    PROJECT_ROOT / "data/assets/b.png"]
    assert F.panel_paths(figure_chunk(asset)) == [(PROJECT_ROOT / asset)]


def test_panels_are_capped_and_prose_has_none(asset):
    many = [f"data/assets/p{i}.png" for i in range(9)]
    assert len(F.panel_paths(figure_chunk(asset, panels=many))) == 4
    assert F.panel_paths(make_chunk(1, PROSE)) == []


def test_an_oversized_image_is_downscaled_to_a_jpeg(tmp_path):
    from PIL import Image

    from contract_analyzer.parse.images import MAX_LONG_EDGE, encode_image

    big = tmp_path / "wide.png"
    Image.new("RGBA", (MAX_LONG_EDGE * 2, 400)).save(big)
    block = encode_image(big)
    assert block["source"]["media_type"] == "image/jpeg"
    import io

    with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as sent:
        assert max(sent.size) == MAX_LONG_EDGE


def test_the_same_panel_twice_is_encoded_once_and_the_setting_turns_it_off(asset):
    two = [figure_chunk(asset, chunk_id=1), figure_chunk(asset, chunk_id=2)]
    assert len(F.figure_blocks(two, settings=settings())) == 1
    assert F.figure_blocks(two, settings=settings(send_figure_images=False)) == []


# --------------------------------------------------------------------------
# The tool result
# --------------------------------------------------------------------------


def test_a_retrieved_figure_puts_an_image_on_the_tool_result(searches, asset):
    searches(figure_chunk(asset))
    outcome = tools().execute("search_contract", {"query": "data flow"})

    assert isinstance(outcome.content, list)
    assert [b["type"] for b in outcome.content] == ["text", "image"]
    # The caption is still the text half, unchanged, and `.text` still reads it.
    assert outcome.content[0]["text"] == outcome.text
    assert CAPTION in outcome.text and outcome.text.startswith("[E1] ")
    # The model is told which id the picture belongs to.
    assert outcome.text.rstrip().endswith("Figure images attached below, in order: E1.")
    source = outcome.content[1]["source"]
    assert source["type"] == "base64" and source["media_type"] == "image/png"
    assert base64.b64decode(source["data"]) == TINY_PNG


def test_two_figures_in_one_result_are_named_in_order(searches, tmp_path):
    import os

    panel = tmp_path / "figure-3-1.png"
    panel.write_bytes(TINY_PNG)
    shared = os.path.relpath(panel, PROJECT_ROOT)
    searches(figure_chunk(shared, chunk_id=1), make_chunk(2, PROSE),
             figure_chunk(shared, chunk_id=3))

    outcome = tools().execute("search_contract", {"query": "data flow"})
    # E3 repeats E1's panel, so one image is sent and only E1 claims it.
    assert [b["type"] for b in outcome.content] == ["text", "image"]
    assert "in order: E1." in outcome.text


def test_a_prose_result_is_still_a_plain_string(searches, asset):
    searches(make_chunk(1, PROSE))
    outcome = tools().execute("search_contract", {"query": "data flow"})
    assert isinstance(outcome.content, str) and outcome.content == outcome.text


def test_a_missing_asset_is_caption_only_and_does_not_raise(searches):
    searches(figure_chunk("data/assets/gone.png"))
    outcome = tools().execute("search_contract", {"query": "data flow"})
    assert isinstance(outcome.content, str)
    assert CAPTION in outcome.content


def test_an_unreadable_figure_does_not_stop_the_agent_loop(searches, tmp_path):
    """A .png that is not one: encoding gives up, the caption still goes."""
    import os

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png at all")
    searches(figure_chunk(os.path.relpath(broken, PROJECT_ROOT)))
    outcome = tools().execute("search_contract", {"query": "data flow"})
    # Pillow cannot open it, so it is dropped rather than sent: garbage bytes
    # would cost the whole request a 400, and the caption still answers.
    assert isinstance(outcome.content, str) and CAPTION in outcome.content

    searches(figure_chunk("data/assets/notes.txt"))  # a suffix the API will not take
    plain = tools().execute("search_contract", {"query": "data flow"})
    assert isinstance(plain.content, str) and CAPTION in plain.content


def test_the_agent_loop_sends_the_block_list_unflattened(searches, asset):
    searches(figure_chunk(asset))
    api = ScriptedAPI(search_turn(), sse_message([{"type": "text", "text": "Found it."}]),
                      sse_message([{"type": "text", "text": "The gateway."}]))
    ask(api)

    # requests[1] is the loop's second turn: it carries the tool result.
    result = api.requests[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result"
    assert [b["type"] for b in result["content"]] == ["text", "image"]
    assert result["content"][1]["source"]["media_type"] == "image/png"


# --------------------------------------------------------------------------
# The chat finisher
# --------------------------------------------------------------------------


def test_the_finisher_appends_the_image_beside_a_text_document_block(searches, asset):
    searches(figure_chunk(asset))
    api = ScriptedAPI(search_turn(), sse_message([{"type": "text", "text": "Found it."}]),
                      sse_message([{"type": "text", "text": "The gateway."}]))
    ask(api)

    content = api.requests[2]["messages"][-1]["content"]
    assert [c["type"] for c in content] == ["document", "image", "text"]
    # The document source stays plain text, so citations stay char_location.
    assert content[0]["source"]["type"] == "text"
    assert CAPTION in content[0]["source"]["data"]
    assert content[0]["citations"] == {"enabled": True}
    assert base64.b64decode(content[1]["source"]["data"]) == TINY_PNG
    assert content[2]["text"] == "Where does personal data go?"


def test_a_contract_with_no_figures_sends_no_images(searches, asset):
    searches(make_chunk(1, PROSE))
    api = ScriptedAPI(search_turn(), sse_message([{"type": "text", "text": "Found it."}]),
                      sse_message([{"type": "text", "text": "The gateway."}]))
    ask(api)
    assert "image" not in json.dumps(api.requests[1]) + json.dumps(api.requests[2])
