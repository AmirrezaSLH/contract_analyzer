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
