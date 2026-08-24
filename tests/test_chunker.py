"""Chunker regression suite: budget, boundaries, overlap and what is dropped.

Unit tests run on synthetic elements and never open a PDF. The tests marked
`needs_sample` pin the numbers measured on the sample contract, in the same
style as the parser suite -- so a change that quietly stops indexing an
exhibit shows up as a failing count rather than as a worse answer.

The rules that actually protect a citation are the boundary ones. A chunk that
spans two sections, or begins mid-sentence, produces an answer whose source is
subtly wrong, and that is worse than one that fails to answer.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

import pytest

from conftest import needs_sample
from contract_analyzer.config import Settings
from contract_analyzer.ingest.chunker import (
    MIN_CHUNK_TOKENS,
    ChunkingReport,
    chunk_document,
    chunk_elements,
)
from contract_analyzer.parse.elements import Element, FigureElement, TableElement
from contract_analyzer.parse.tables import rows_to_markdown
from contract_analyzer.tokens import count_tokens

BBOX = (72.0, 100.0, 540.0, 140.0)

SECTION = ("6. Identity, Access, Authentication", "6.6 Password Management Standard")

#: Contract vocabulary, so a chunk's provenance is readable in a failure
#: message and substring assertions cannot match by accident.
_WORDS = [
    "vendor", "supplier", "customer", "obligation", "confidential", "encryption",
    "credential", "privileged", "rotation", "retention", "notification", "remediation",
    "subcontractor", "indemnity", "warranty", "termination", "compliance", "attestation",
]


def settings(**overrides) -> Settings:
    # Explicit values, never the .env ones: a test that changes meaning when
    # someone edits CHUNK_TOKENS is not a test.
    overrides.setdefault("chunk_tokens", 200)
    overrides.setdefault("chunk_overlap_tokens", 40)
    return Settings(**overrides)


def sentence(tokens: int, tag: str) -> str:
    """One sentence of roughly `tokens` tokens, ending in a distinctive tag."""
    body = " ".join(_WORDS[i % len(_WORDS)] for i in range(tokens))
    while count_tokens(f"{body} {tag}.") > tokens:
        body = body.rsplit(" ", 1)[0]
    return f"{body} {tag}."


def clause(tag: str, *, sentences: int = 4, tokens: int = 20) -> str:
    """A clause of `sentences` whole sentences, each individually identifiable."""
    return " ".join(sentence(tokens, f"{tag}{i}") for i in range(sentences))


def para(text: str, *, path=SECTION, page=0, label="1", page_end=None, label_end="") -> Element:
    return Element(
        type="paragraph",
        text=text,
        page_index=page,
        page_label=label,
        page_end=page_end,
        page_label_end=label_end,
        bbox=BBOX,
        section=path[-1] if path else "",
        section_path=list(path),
    )


def heading(text: str, *, path=SECTION, page=0, label="1") -> Element:
    return Element(
        type="heading", text=text, page_index=page, page_label=label, bbox=BBOX,
        section=path[-1] if path else "", section_path=list(path),
    )


def equation(text: str = "E = t * C", **kw) -> Element:
    element = para(text, **kw)
    element.type = "equation"
    return element


def table(rows: list[list[str]], caption: str = "", **kw) -> TableElement:
    """A contract table. The caption defaults to empty, as Word contracts have."""
    markdown = rows_to_markdown(rows)
    common = {
        "path": ("Exhibit G — Security Schedule", "G1. Governance"),
        "page": 13,
        "label": "14",
    }
    common.update(kw)
    path = common["path"]
    return TableElement(
        text=f"{caption}\n{markdown}".strip() if caption else markdown,
        page_index=common["page"],
        page_label=common["label"],
        page_end=common.get("page_end"),
        page_label_end=common.get("label_end", ""),
        bbox=BBOX,
        section=path[-1] if path else "",
        section_path=list(path),
        markdown=markdown,
        rows=rows,
        caption=caption,
        quality="ruled",
    )


def figure(caption: str = "Figure 1: Network segmentation", **kw) -> FigureElement:
    common = {"path": ("Exhibit B — Architecture",), "page": 6, "label": "7",
              "assets": ["data/assets/contract/p1.png"]}
    common.update(kw)
    path = common["path"]
    return FigureElement(
        text=caption,
        page_index=common["page"],
        page_label=common["label"],
        bbox=BBOX,
        section=path[-1] if path else "",
        section_path=list(path),
        caption=caption,
        asset_paths=[Path(p) for p in common["assets"]],
    )


def body_of(chunk) -> str:
    """A chunk's text without the breadcrumb line every chunk opens with."""
    return chunk.content.split("\n", 1)[1]


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------


def test_no_chunk_exceeds_the_token_budget():
    """The breadcrumb is paid for out of the budget, not added on top of it."""
    config = settings()
    elements = [para(clause(f"c{i}")) for i in range(12)]
    chunks = chunk_elements(elements, config)

    assert len(chunks) > 1, "twelve 80-token clauses must not fit in one 200-token chunk"
    for c in chunks:
        assert c.token_count <= config.chunk_tokens, c.content[:80]
        assert c.content.startswith(" > ".join(SECTION) + "\n")


def test_an_oversized_element_is_split_at_sentence_ends():
    config = settings()
    text = clause("long", sentences=20)
    report = ChunkingReport()
    chunks = chunk_elements([para(text)], config, report=report)

    assert len(chunks) > 1
    assert report.oversized_elements_split == 1
    for c in chunks:
        assert c.token_count <= config.chunk_tokens
        # Split at a sentence end, so every part still ends in a full stop.
        assert c.content.rstrip().endswith(".")


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


def test_a_section_change_closes_a_chunk():
    """Two clauses in one chunk make its citation a lie about half its text."""
    a = para(clause("a", sentences=2), path=("6. Identity", "6.6 Password Management"))
    b = para(clause("b", sentences=2), path=("6. Identity", "6.7 Account Lockout"))
    chunks = chunk_elements([a, a, b, b], settings())

    assert len(chunks) == 2
    assert chunks[0].section == "6.6 Password Management"
    assert chunks[1].section == "6.7 Account Lockout"
    assert "6.7" not in chunks[0].content
    assert "6.6" not in body_of(chunks[1])


def test_overlap_carries_whole_elements_when_they_fit():
    """No chunk begins mid-sentence, because overlap starts at an element."""
    texts = [clause(f"e{i}", sentences=1, tokens=30) for i in range(14)]
    chunks = chunk_elements([para(t) for t in texts], settings())

    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        body = body_of(current)
        carried = [t for t in texts if body.startswith(t)]
        assert carried, "a chunk started with something that is not a whole element"
        assert carried[0] in previous.content


def test_overlap_falls_back_to_the_trailing_sentences_of_a_big_clause():
    """A clause too big to carry whole contributes its tail, not nothing.

    This is the rule that makes the configured overlap real on a contract. The
    parser emits one element per clause, so a strict whole-element rule carries
    nothing whenever the trailing clause is larger than the overlap budget --
    which for a 400-token budget and a 290-token clause is the normal case, not
    the corner one.
    """
    config = settings()  # 200-token budget, 40-token overlap
    first = para(clause("alpha", sentences=4, tokens=20))
    second = para(clause("beta", sentences=4, tokens=20))
    third = para(clause("gamma", sentences=4, tokens=20))
    report = ChunkingReport()
    chunks = chunk_elements([first, second, third], config, report=report)

    assert len(chunks) == 2
    carried = body_of(chunks[1]).split("\n")[0]
    assert carried, "the second chunk carried no overlap at all"
    # A tail of the previous clause, not the whole of it, and not a cut sentence.
    assert second.text.endswith(carried)
    assert carried != second.text
    assert carried.endswith("beta3.")
    assert count_tokens(carried) <= config.chunk_overlap_tokens
    assert report.overlap_chunks == 1
    assert report.overlap_tokens == count_tokens(carried)


def test_overlap_is_never_a_partial_sentence():
    """If even the last sentence is over budget, nothing is carried."""
    config = settings(chunk_overlap_tokens=10)
    elements = [para(clause(t, sentences=4, tokens=20)) for t in ("alpha", "beta", "gamma")]
    report = ChunkingReport()
    chunks = chunk_elements(elements, config, report=report)

    assert len(chunks) == 2
    assert report.overlap_chunks == 0
    assert body_of(chunks[1]).startswith(elements[2].text[:40])


def test_overlap_never_carries_a_whole_chunk():
    """Packing has to advance: every chunk must contain something new."""
    texts = [clause(f"e{i}", sentences=1, tokens=30) for i in range(14)]
    chunks = chunk_elements([para(t) for t in texts], settings())

    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.content != previous.content
        tail = current.content.rsplit("\n", 1)[-1]
        assert tail not in previous.content


def test_page_comes_from_the_first_non_overlap_element():
    """Overlap supplies context, never the citation."""
    # Three 60-token clauses fill the budget exactly, so the chunk closes on
    # the page change rather than somewhere in the middle of the first page.
    early = [para(clause(f"a{i}", sentences=3), page=0, label="1") for i in range(3)]
    late = [para(clause(f"b{i}", sentences=3), page=9, label="10") for i in range(2)]
    chunks = chunk_elements([*early, *late], settings())

    assert len(chunks) == 2
    assert chunks[0].page == 0 and chunks[0].page_label == "1"
    # The second chunk opens with overlap from page 0, but the first element
    # that is genuinely its own is on page 9.
    assert chunks[1].page == 9 and chunks[1].page_label == "10"


def test_a_chunk_cites_the_page_range_its_own_text_reaches():
    """A clause the parser rejoined across a page break says p.9-10."""
    spanning = para(clause("span", sentences=2), page=8, label="9", page_end=9, label_end="10")
    chunks = chunk_elements([spanning], settings())

    assert len(chunks) == 1
    assert chunks[0].page == 8
    assert chunks[0].page_end == 9
    assert chunks[0].page_display == "9-10"


def test_overlap_does_not_widen_the_cited_page_range():
    """Borrowed context must not make a citation claim a page it is not on."""
    config = settings()
    early = [para(clause(f"a{i}", sentences=3), page=0, label="1") for i in range(3)]
    late = [para(clause(f"b{i}", sentences=3), page=9, label="10") for i in range(2)]
    report = ChunkingReport()
    chunks = chunk_elements([*early, *late], config, report=report)

    assert len(chunks) == 2
    assert report.overlap_chunks == 1, "this test is only meaningful if overlap happened"
    # The second chunk opens with page-0 overlap; its range starts at page 9
    # and does not run backwards to meet the borrowed text.
    assert chunks[1].page == 9
    assert chunks[1].page_end is None
    assert chunks[1].page_display == "10"


def test_a_single_page_chunk_leaves_the_range_unset():
    chunks = chunk_elements([para(clause("one", sentences=2))], settings())

    assert chunks[0].page_end is None
    assert chunks[0].page_label_end == ""
    assert chunks[0].page_display == "1"


# --------------------------------------------------------------------------
# Headings and equations
# --------------------------------------------------------------------------


def test_a_heading_is_a_breadcrumb_not_a_chunk():
    report = ChunkingReport()
    chunks = chunk_elements(
        [heading("6.6 Password Management Standard"), para(clause("a", sentences=2))],
        settings(),
        report=report,
    )

    assert report.headings == 1
    assert len(chunks) == 1
    assert chunks[0].element_type == "paragraph"
    # The heading's text survives where it is useful: in the opening line of
    # every chunk, rather than as a near-empty row competing in BM25.
    assert chunks[0].content.startswith(" > ".join(SECTION) + "\n")


def test_an_equation_is_never_a_chunk_on_its_own():
    chunks = chunk_elements(
        [para(clause("a", sentences=3)), equation(), equation("C = r * n")], settings()
    )

    assert len(chunks) == 1
    assert chunks[0].element_type == "paragraph"
    assert "E = t * C" in chunks[0].content
    assert "C = r * n" in chunks[0].content


def test_a_trailing_equation_does_not_open_a_new_chunk():
    elements = [para(clause(f"c{i}")) for i in range(4)] + [equation()]
    chunks = chunk_elements(elements, settings())

    assert all(c.element_type == "paragraph" for c in chunks)
    assert "E = t * C" in chunks[-1].content


# --------------------------------------------------------------------------
# Tables -- where a compliance answer usually comes from
# --------------------------------------------------------------------------


def test_a_table_chunk_opens_with_its_breadcrumb_and_keeps_bare_markdown():
    """The fix that makes a requirements matrix findable by what it requires.

    The cells say "Password rotation"; they never say "Password Management
    Standard". Prefixing the section is what lets BM25 reach the row from the
    section name, and it is why the payload has to stay the bare grid -- the UI
    renders a table, not a heading with a table under it.
    """
    rows = [
        ["ID", "Requirement", "Minimum Standard"],
        ["PASS-01", "Password rotation", "90 days"],
        ["PASS-02", "Break-glass credentials", "Sealed, rotated on use"],
    ]
    grid = table(rows, path=("Exhibit G", "G3A. Password Management"))
    chunks = chunk_elements([grid], settings())

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.element_type == "table"
    assert chunk.content.startswith("Exhibit G > G3A. Password Management\n")
    assert chunk.payload == rows_to_markdown(rows)
    assert not chunk.payload.startswith("Exhibit G")
    assert "PASS-01" in chunk.content


def test_a_table_is_atomic_and_closes_the_prose_around_it():
    rows = [["ID", "Control"], ["GOV-01", "Written program"], ["GOV-02", "Annual review"]]
    chunks = chunk_elements(
        [para(clause("a", sentences=2)), table(rows), para(clause("b", sentences=2))], settings()
    )

    assert [c.element_type for c in chunks] == ["paragraph", "table", "paragraph"]


def test_an_oversized_table_splits_by_rows_with_the_header_repeated():
    config = settings()
    rows = [["ID", "Requirement", "Minimum Standard", "Evidence"]]
    rows += [[f"NET-{i:02d}", f"Control {i}", f"Standard {i}", f"Report {i}"] for i in range(40)]
    report = ChunkingReport()
    chunks = chunk_elements([table(rows)], config, report=report)

    assert len(chunks) > 1
    assert report.tables_split == 1
    for index, c in enumerate(chunks, start=1):
        assert c.element_type == "table"
        assert c.token_count <= config.chunk_tokens
        # Every part must be readable alone: header, and which part it is.
        assert rows_to_markdown([rows[0]]).splitlines()[0] in c.payload
        assert f"(part {index}/{len(chunks)})" in c.content
    # Every body row survives the split exactly once.
    for row in rows[1:]:
        assert sum(c.content.count(f"|{row[0]}|") for c in chunks) == 1


def test_a_stitched_table_cites_the_pages_it_spans():
    rows = [["ID", "Control"], ["GOV-01", "Written program"], ["GOV-02", "Annual review"]]
    chunks = chunk_elements(
        [table(rows, page=13, label="14", page_end=14, label_end="15")], settings()
    )

    assert chunks[0].page_display == "14-15"


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------


def test_a_figure_chunk_carries_its_caption_and_the_prose_that_cites_it():
    mention = para(
        "Figure 1 shows the segmentation boundary between the vendor network "
        "and the customer production environment.",
        path=("Exhibit B — Architecture",),
    )
    chunks = chunk_elements([figure(), mention], settings())

    figures = [c for c in chunks if c.element_type == "figure"]
    assert len(figures) == 1
    assert "Figure 1: Network segmentation" in figures[0].content
    assert "segmentation boundary" in figures[0].content
    assert figures[0].asset_path == "data/assets/contract/p1.png"
    assert json.loads(figures[0].payload)["panels"] == ["data/assets/contract/p1.png"]


def test_a_figure_without_a_mention_is_still_a_chunk():
    report = ChunkingReport()
    chunks = chunk_elements([figure()], settings(), report=report)

    assert len(chunks) == 1
    assert report.figures_total == 1
    assert report.figures_with_reference == 0


# --------------------------------------------------------------------------
# What gets dropped -- and, mostly, what does not
# --------------------------------------------------------------------------


def test_contents_rows_are_dropped():
    row = para("6.6 Password Management Standard . . . . . . . . . . 14", path=("Contents",))
    report = ChunkingReport()
    chunks = chunk_elements([row, para(clause("a", sentences=2))], settings(), report=report)

    assert report.dropped_contents == 1
    assert all("Standard . . ." not in c.content for c in chunks)


def test_an_exhibit_is_never_dropped_by_its_title():
    """A contract's exhibits are the evidence, not an appendix to skip.

    The corpus this chunker descends from dropped "References" and
    "Bibliography" by name, which was safe there and would be fatal here:
    Exhibit G is where the numbered password requirements live.
    """
    elements = [
        para(clause("gov", sentences=2), path=("Exhibit G — Security Schedule", "G1. Governance")),
        para(clause("def", sentences=2), path=("1. Definitions",)),
        para(clause("sch", sentences=2), path=("Schedule 2 — Service Levels",)),
        para(clause("ann", sentences=2), path=("Annex A — Subprocessors",)),
    ]
    report = ChunkingReport()
    chunks = chunk_elements(elements, settings(), report=report)

    assert len(chunks) == 4
    assert report.dropped_front_matter == 0
    assert report.dropped_contents == 0


def test_a_front_matter_section_is_dropped_only_when_the_outline_named_it():
    """A synthesized spine is not evidence that a section is front matter.

    With no outline, a "Contents" heading owns every element until the next
    heading the synthesizer managed to detect -- which on a contract is real
    clauses. The PDF's own outline says where a section ends, so it can be
    trusted; inferred structure cannot.
    """
    element = para(clause("body", sentences=2), path=("Table of Contents",))

    from_outline = ChunkingReport()
    assert chunk_elements([element], settings(), spine_source="outline",
                          report=from_outline) == []
    assert from_outline.dropped_front_matter == 1

    synthesized = ChunkingReport()
    kept = chunk_elements([element], settings(), spine_source="headings", report=synthesized)
    assert len(kept) == 1
    assert synthesized.dropped_front_matter == 0


def test_furniture_never_reaches_a_chunk():
    running_header = para("Master Services Agreement — Page 4 of 21")
    running_header.type = "furniture"
    chunks = chunk_elements([running_header, para(clause("a", sentences=2))], settings())

    assert all("Page 4 of 21" not in c.content for c in chunks)


def test_a_one_sentence_clause_survives_the_minimum_length_floor():
    """A floor tuned for prose paragraphs would delete a real obligation."""
    short = para("Either party may terminate this Agreement on thirty days written notice.")
    assert count_tokens(short.text) >= MIN_CHUNK_TOKENS

    report = ChunkingReport()
    chunks = chunk_elements([short], settings(), report=report)

    assert len(chunks) == 1
    assert report.dropped_short_chunks == 0
    assert "thirty days" in chunks[0].content


def test_a_fragment_shorter_than_the_floor_is_dropped_and_counted():
    report = ChunkingReport()
    chunks = chunk_elements([para("(a)")], settings(), report=report)

    assert chunks == []
    assert report.dropped_short_chunks == 1


# --------------------------------------------------------------------------
# Determinism -- what idempotent ingestion rests on
# --------------------------------------------------------------------------


def test_chunking_is_deterministic():
    elements = [
        heading("6.6 Password Management Standard"),
        para(clause("a", sentences=3)),
        table([["ID", "Control"], ["GOV-01", "x"], ["GOV-02", "y"]]),
        figure(),
        para(clause("b", sentences=3), path=("6. Identity", "6.7 Account Lockout")),
    ]
    first = chunk_elements(elements, settings())
    second = chunk_elements(elements, settings())

    assert [vars(c) for c in first] == [vars(c) for c in second]
    assert [c.ordinal for c in first] == list(range(len(first)))


@pytest.mark.parametrize("budget", [120, 200, 600])
def test_ordinals_are_dense_and_start_at_zero(budget):
    elements = [para(clause(f"c{i}")) for i in range(10)]
    chunks = chunk_elements(elements, settings(chunk_tokens=budget))

    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


# --------------------------------------------------------------------------
# The sample contract -- the numbers this chunker is claimed to produce
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sample_chunks(sample):
    report = ChunkingReport()
    config = Settings(chunk_tokens=400, chunk_overlap_tokens=80)
    return chunk_document(sample, config, report=report), report


@needs_sample
def test_sample_chunk_counts(sample_chunks):
    chunks, report = sample_chunks

    assert len(chunks) == 102
    kinds = Counter(c.element_type for c in chunks)
    assert kinds == {"paragraph": 67, "table": 35}
    assert report.elements_in == 164
    assert report.headings == 51


@needs_sample
def test_sample_drops_nothing_that_matters(sample_chunks):
    """The one dropped chunk is a section title that survives as a breadcrumb."""
    chunks, report = sample_chunks

    assert report.dropped_contents == 0
    assert report.dropped_front_matter == 0
    assert report.oversized_elements_split == 0
    assert report.tables_split == 1
    # "5.3 Control Objectives." -- a paragraph whose entire text is its own
    # section title, already carried by the breadcrumb of the table beneath it.
    assert report.dropped_short_chunks == 1
    assert any(c.section.startswith("5.3") for c in chunks)


@needs_sample
def test_sample_every_chunk_is_placed_and_within_budget(sample_chunks):
    chunks, _ = sample_chunks

    assert all(c.section_path for c in chunks)
    assert all(c.content.startswith(c.breadcrumb + "\n") for c in chunks)
    assert max(c.token_count for c in chunks) <= 400
    tokens = sorted(c.token_count for c in chunks)
    assert statistics.median(tokens) < 120
    assert tokens[int(0.95 * len(tokens))] <= 400


@needs_sample
def test_sample_the_clauses_a_reviewer_asks_about_each_open_a_chunk(sample_chunks):
    chunks, _ = sample_chunks

    for clause_number in ("5.3", "6.2", "6.6", "7.2", "9.1", "9.2", "9.3"):
        assert any(c.section.startswith(clause_number) for c in chunks), clause_number


@needs_sample
def test_sample_every_requirement_identifier_reaches_the_index(sample_chunks):
    """The parser fix that recovered `GOV-01` from `GOV- 01`, asserted end to end."""
    chunks, _ = sample_chunks
    found = set()
    for c in chunks:
        found |= set(re.findall(r"\b(?:GOV|PASS|ENC|NET|TRN|AST)-\d\d\b", c.content))

    assert "GOV-01" in found
    assert len(found) >= 12
    gov = next(c for c in chunks if "GOV-01" in c.content)
    assert gov.element_type == "table"
    assert gov.breadcrumb.startswith("Exhibit G")


@needs_sample
def test_sample_overlap_is_configured_but_never_exercised(sample_chunks):
    """An honest negative: no section here is big enough for the budget to bite.

    A section boundary closes every chunk on this contract long before 400
    tokens do, so nothing is ever carried forward. The overlap rules are still
    correct -- `test_overlap_falls_back_to_the_trailing_sentences_of_a_big_clause`
    proves that on synthetic elements -- but claiming this document exercises
    them would be claiming something that is not true.
    """
    _, report = sample_chunks

    assert report.overlap_chunks == 0
    assert report.overlap_tokens == 0
