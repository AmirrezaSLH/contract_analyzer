"""Shared fixtures.

The sample contract is not committed (it lands with the fixture commit), so
every test that reads it is skipped when the file is absent. Parsing takes
about a second, so it is done once per session.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = ROOT / "data" / "samples" / "Sample Contract.pdf"

needs_sample = pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample contract not present")


@pytest.fixture(scope="session")
def sample():
    """The sample contract, parsed once."""
    if not SAMPLE_PDF.exists():
        pytest.skip("sample contract not present")
    from contract_analyzer.parse import parse_pdf

    return parse_pdf(SAMPLE_PDF, extract_figures=False)


def make_profile(
    *,
    words: list[str] | None = None,
    hyphenated: list[str] | None = None,
    breaks_hyphenate: bool = True,
    paragraph_indent: float = 0.0,
):
    """A synthetic document profile for unit tests that never open a PDF."""
    from contract_analyzer.parse.blocks import DocumentProfile

    return DocumentProfile(
        body_size=12.0,
        page_count=1,
        body_left=36.0,
        body_right=577.0,
        words=Counter(words or []),
        hyphenated=Counter(hyphenated or []),
        breaks_hyphenate=breaks_hyphenate,
        paragraph_indent=paragraph_indent,
    )


@dataclass
class Corpus:
    """Two contracts in one database. Scoping cannot be tested with one."""

    conn: object
    settings: object
    embedder: object
    sample_id: int
    decoy_id: int
    decoy_token: str
    decoy_path: Path


#: A word that appears in the decoy and nowhere in the sample contract. A
#: scoped search that returns it has leaked across documents.
DECOY_TOKEN = "zephyrine"

#: The decoy repeats the sample's password vocabulary verbatim, so under any
#: embedder the decoy's chunks are the *nearest* ones to a password question.
#: That is what makes "scoped KNN is exact, not filtered after the fact"
#: falsifiable: a global top-k would be all decoy.
_DECOY_CLAUSE = (
    "Supplier shall enforce password rotation, break-glass credentials, "
    "privileged password vaulting and password complexity for every account, "
    "and shall record each password rotation event. Reference {token}-{index}."
)


def write_decoy_contract(path: Path, *, pages: int = 2):
    """A second, synthetic contract: parseable, and deliberately confusable."""
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text(
            (72, 90),
            f"{index + 3}. Section {index + 3} Password Controls",
            fontsize=16,
            fontname="hebo",
        )
        top = 130.0
        for k in range(5):
            page.insert_textbox(
                pymupdf.Rect(72, top, 520, top + 80),
                f"{index + 3}.{k + 1} Clause {k + 1}. "
                + _DECOY_CLAUSE.format(token=DECOY_TOKEN, index=f"{index}{k}"),
                fontsize=11,
            )
            top += 90
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def ingested_sample(tmp_path_factory) -> Corpus:
    """The sample contract plus the decoy, ingested once with `FakeEmbedder`.

    Session-scoped because ingesting the sample costs a parse; every test here
    only reads. `fake` embeddings mean no network and no key -- they have no
    semantics, so the vector assertions are about *scope and mechanics*, which
    is all a hashed embedder can honestly support.
    """
    pytest.importorskip("pymupdf")
    if not SAMPLE_PDF.exists():
        pytest.skip("sample contract not present")

    from contract_analyzer.config import Settings
    from contract_analyzer.db import get_db
    from contract_analyzer.embeddings.fake import FakeEmbedder
    from contract_analyzer.ingest.pipeline import ingest_file

    tmp = tmp_path_factory.mktemp("corpus")
    settings = Settings(
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=64,
        db_path=tmp / "contracts.db",
        raw_dir=tmp,
        assets_dir=tmp / "assets",
        log_file=None,
    )
    conn = get_db(settings)
    embedder = FakeEmbedder(settings)

    sample = ingest_file(SAMPLE_PDF, conn, embedder, settings)
    decoy_path = write_decoy_contract(tmp / "decoy.pdf")
    decoy = ingest_file(decoy_path, conn, embedder, settings)
    assert sample.ok and decoy.ok, (sample.error, decoy.error)

    return Corpus(
        conn=conn,
        settings=settings,
        embedder=embedder,
        sample_id=sample.document_id,
        decoy_id=decoy.document_id,
        decoy_token=DECOY_TOKEN,
        decoy_path=decoy_path,
    )


# --------------------------------------------------------------------------
# A scripted Anthropic API: canned SSE through MockTransport, real SDK on top
# --------------------------------------------------------------------------


def sse_message(
    blocks: list[dict],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 20,
    model: str = "claude-opus-5",
) -> str:
    """The SSE body for one streamed message.

    `blocks` are final content blocks: `{"type": "text", "text": ..., "citations": [...]}`
    or `{"type": "tool_use", "id": ..., "name": ..., "input": {...}}`. They are
    emitted the way the API streams them -- text as deltas, tool input as
    `input_json_delta`, citations as `citations_delta` -- so the SDK's own
    assembly is what the tests exercise.
    """
    import json

    def event(name: str, payload: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(payload)}\n\n"

    out = event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
    )
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            out += event(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "text", "text": ""}},
            )
            text = block["text"]
            for start in range(0, len(text), 7):
                out += event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "text_delta", "text": text[start : start + 7]}},
                )
            for citation in block.get("citations") or []:
                out += event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index,
                     "delta": {"type": "citations_delta", "citation": citation}},
                )
        elif block["type"] == "tool_use":
            out += event(
                "content_block_start",
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "tool_use", "id": block["id"],
                                   "name": block["name"], "input": {}}},
            )
            out += event(
                "content_block_delta",
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "input_json_delta",
                           "partial_json": json.dumps(block["input"])}},
            )
        else:
            raise ValueError(block["type"])
        out += event("content_block_stop", {"type": "content_block_stop", "index": index})
    out += event(
        "message_delta",
        {"type": "message_delta",
         "delta": {"type": "message_delta", "stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": output_tokens}},
    )
    out += event("message_stop", {"type": "message_stop"})
    return out


class ScriptedAPI:
    """Each request pops the next scripted outcome: an SSE body, a status int, or an exception.

    Every request's JSON body is kept in `requests` so a test can assert on
    exactly what the SDK sent -- tools, output_config, document blocks.
    """

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict] = []

    def __call__(self, request):
        import json

        import httpx2 as httpx

        self.requests.append(json.loads(request.content or b"{}"))
        if not self.outcomes:
            raise AssertionError("the script ran out of responses")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return httpx.Response(
                outcome,
                json={"type": "error", "error": {"type": "error", "message": f"HTTP {outcome}"}},
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=outcome.encode()
        )

    @property
    def calls(self) -> int:
        return len(self.requests)


def scripted_client(api: ScriptedAPI, *, retries: int = 0):
    """A real `anthropic.Anthropic` on the project's transport, over the script."""
    import httpx2 as httpx
    from anthropic import Anthropic

    from contract_analyzer.http_client import build_http_client

    http = build_http_client(transport=httpx.MockTransport(api), retries=retries, backoff_base=0.0)
    return Anthropic(api_key="test-key", http_client=http, max_retries=0)


def clean_findings(criterion, *, quote_index: int = 0, confidence: float = 0.85) -> str:
    """An all-agree `EvaluatorFindings` for a draft whose sub-requirements all
    cite the same quote.

    Every path through the analysis now ends with a critic call, so every
    scripted run needs one more reply than it used to. The disagreement cases
    are authored in `test_evaluator.py` and `test_router.py`, where they are
    the subject; here the critic exists so the pipeline can run, and one helper
    keeps that from being five lines in every test.
    """
    import json

    return json.dumps({
        "quote_support": [
            {"quote_index": quote_index, "sub_requirement_id": sub.id,
             "support": "supports", "note": "carries the claim"}
            for sub in criterion.sub_requirements
        ],
        "status_agreement": [
            {"sub_requirement_id": sub.id, "agreement": "agree", "note": ""}
            for sub in criterion.sub_requirements
        ],
        "state_agreement": "agree",
        "missing_searches": [],
        "critic_confidence": confidence,
        "notes": "",
    })


def critic_turn(criterion, **kw) -> str:
    """The scripted SSE body for one clean evaluation."""
    return sse_message([{"type": "text", "text": clean_findings(criterion, **kw)}])


def make_chunk(chunk_id: int, text: str, *, section: str = "6.6 Password Management Standard",
               page: str = "9", document_id: int = 1, element_type: str = "paragraph",
               payload: str | None = None):
    """A `RetrievedChunk` with no database behind it."""
    from contract_analyzer.retrieval.base import RetrievedChunk

    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        ordinal=chunk_id,
        content=text,
        filename="Sample Contract.pdf",
        page=int(page),
        page_label=page,
        section=section,
        section_path=["6. Identity and Access Management", section],
        element_type=element_type,
        payload=payload,
        spine_source="outline",
    )
