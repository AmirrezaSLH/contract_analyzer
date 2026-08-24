"""Parser regression suite: text elements, clause structure and the section spine.

Unit tests run on synthetic elements and never open a PDF. The tests marked
`needs_sample` assert the measured targets of the parser hardening plan on
the sample contract and are skipped when the file is absent.
"""

from __future__ import annotations

import re
import time

import pytest
from contract_analyzer.parse.enumerators import EnumeratorLattice, match_enumerator

from conftest import SAMPLE_PDF, make_profile, needs_sample
from contract_analyzer.parse.blocks import (
    infer_breaks_hyphenate,
    is_page_number,
    join_lines,
    paragraph_indent,
)
from contract_analyzer.parse.elements import Element, TableElement
from contract_analyzer.parse.outline import assign_sections, synthesize_spine
from contract_analyzer.parse.pdf import join_wrapped_lines, split_welded

# --------------------------------------------------------------------------
# helpers


def para(text: str, *, page: int = 0, y: float = 100.0, x0: float = 36.0, x1: float = 577.0):
    return Element(
        type="paragraph",
        text=text,
        page_index=page,
        page_label=str(page + 1),
        bbox=(x0, y, x1, y + 12.0),
    )


def heading(text: str, *, page: int = 0, y: float = 100.0):
    return Element(
        type="heading",
        text=text,
        page_index=page,
        page_label=str(page + 1),
        bbox=(36, y, 300, y + 14),
    )


# --------------------------------------------------------------------------
# P3 -- line-break hyphens


def test_letter_digit_hyphen_is_kept_without_attestation():
    profile = make_profile()  # empty vocabulary: nothing attests GOV-01
    assert join_lines(["GOV-", "01"], profile) == "GOV-01"
    assert join_lines(["ISO-", "27001"], profile) == "ISO-27001"
    assert join_lines(["RA-", "2026-001"], profile) == "RA-2026-001"


def test_attested_compound_keeps_hyphen():
    profile = make_profile(hyphenated=["single-family"], words=["single", "family"])
    assert join_lines(["single-", "family"], profile) == "single-family"


def test_attested_merged_word_drops_hyphen():
    profile = make_profile(words=["building"], breaks_hyphenate=False)
    assert join_lines(["build-", "ing"], profile) == "building"


def test_unattested_hyphen_follows_measured_default():
    keep = make_profile(breaks_hyphenate=False)  # a Word document: breaks are lexical
    drop = make_profile(breaks_hyphenate=True)  # a LaTeX document: breaks are typographic
    assert join_lines(["just-in-", "time"], keep) == "just-in-time"
    assert join_lines(["token-", "based"], keep) == "token-based"
    assert join_lines(["typo-", "graphic"], drop) == "typographic"


def test_hard_wrap_without_hyphen_is_closed_when_the_word_is_attested():
    profile = make_profile(words=["monitoring", "alerting", "requirement", "ref"])
    assert join_lines(["Monitoring/Alertin", "g"], profile) == "Monitoring/Alerting"
    assert join_lines(["Requiremen", "t Ref"], profile) == "Requirement Ref"


def test_hard_wrap_rule_leaves_real_words_alone():
    profile = make_profile(words=["special", "handling", "a", "gain", "again"])
    assert join_lines(["Special", "Handling"], profile) == "Special Handling"
    # Both fragments are words in their own right: not a wrap, whatever the
    # concatenation happens to spell.
    assert join_lines(["a", "gain"], profile) == "a gain"


def test_breaks_hyphenate_is_measured_from_the_evidence():
    words = {"building": 3, "single": 2, "family": 2}
    hyphenated = {"single-family": 2}
    # merged forms dominate: the document auto-hyphenates at line ends
    breaks = [("build", "ing")] * 4 + [("single", "family")]
    assert infer_breaks_hyphenate(breaks, hyphenated, words)
    # compounds dominate: the document does not
    assert not infer_breaks_hyphenate(
        [("single", "family")] * 3 + [("build", "ing")], hyphenated, words
    )
    # no evidence at all: keep the hyphen, which is the reversible choice
    assert not infer_breaks_hyphenate([], {}, {})


# --------------------------------------------------------------------------
# P6 -- furniture


@pytest.mark.parametrize("text", ["71", "xiv", "IV", "1"])
def test_page_numbers_are_recognised(text):
    assert is_page_number(text)


@pytest.mark.parametrize("text", ["LLC", "civil", "did", "MID", "vivid", "iiii", "Page"])
def test_words_made_of_roman_letters_are_not_page_numbers(text):
    assert not is_page_number(text)


def test_paragraph_indent_is_measured_not_assumed():
    from collections import Counter

    # Every full-width block starts at the margin: the document does not indent.
    flat = Counter({(36.0, 577.0): 40})
    assert paragraph_indent(flat, 36.0, 577.0) == 0.0
    # A quarter of the full-width blocks start 18pt in: a LaTeX \parindent.
    indented = Counter({(36.0, 577.0): 30, (54.0, 577.0): 10})
    assert paragraph_indent(indented, 36.0, 577.0) == 18.0
    # A single stray block is not a convention.
    stray = Counter({(36.0, 577.0): 60, (54.0, 577.0): 1})
    assert paragraph_indent(stray, 36.0, 577.0) == 0.0


# --------------------------------------------------------------------------
# P2 -- enumerators and clause boundaries


def test_match_enumerator_shapes():
    e = match_enumerator("6.6 Password Management Standard. Vendor will")
    assert (e.label, e.kind, e.parent, e.depth) == ("6.6", "decimal", "6", 2)
    e = match_enumerator("12.4.1 Sub-sub clause. Text")
    assert (e.label, e.parent, e.depth) == ("12.4.1", "12.4", 3)
    e = match_enumerator("21. Term; Survival")
    assert (e.label, e.kind, e.parent, e.depth) == ("21", "integer", "", 1)
    e = match_enumerator("G3A. Password Management (Added)")
    assert (e.label, e.kind, e.parent, e.depth) == ("G3A", "alnum", "G", 2)
    e = match_enumerator("Exhibit G — Security Schedule")
    assert (e.label, e.kind, e.key, e.depth) == ("Exhibit G", "exhibit", "G", 1)
    e = match_enumerator("(a) Form of Acceptance. Any Risk")
    assert (e.label, e.kind) == ("(a)", "lettered")
    e = match_enumerator("(iv) fourth item")
    assert (e.label, e.kind) == ("(iv)", "roman")


@pytest.mark.parametrize(
    "text",
    [
        "SAML 2.0 SSO is supported",
        "Vendor will enforce MFA",
        "2029 was the year",
        "1.2 is the minimum TLS version",  # no title: a bare number, not a clause
        "A. B",
    ],
)
def test_prose_starts_are_not_enumerators(text):
    assert match_enumerator(text) is None


def test_lattice_corroborates_by_sequence():
    elements = [
        heading("6. Identity", y=10),
        para("6.1 Least Privilege. Vendor will.", y=30),
        para("6.2 MFA. Vendor will enforce SAML 2.0 where supported.", y=50),
        para("6.3 Privileged Access. Vendor will.", y=70),
        para("2.0 is not a clause on its own.", y=90),
        para("Alone. 9.4 Nothing precedes or follows this one.", y=110),
    ]
    lattice = EnumeratorLattice.from_elements(elements)
    assert {"6.1", "6.2", "6.3"} <= lattice.corroborated
    assert "2.0" not in lattice.corroborated
    assert "9.4" not in lattice.corroborated
    assert lattice.opens(elements[1]).label == "6.1"
    assert lattice.opens(elements[4]) is None


def test_lattice_accepts_restarting_lettered_sequences():
    elements = [
        para("(a) First.", y=10),
        para("(b) Second.", y=20),
        para("(a) First again, under a different clause.", y=30),
        para("(b) Second again.", y=40),
    ]
    lattice = EnumeratorLattice.from_elements(elements)
    assert all(lattice.opens(e) is not None for e in elements)


def test_lattice_finds_mid_text_positions_only_after_a_terminator():
    welded = para(
        "6.2 MFA. Vendor will enforce MFA for (a) privileged access. "
        "6.3 Privileged Access Management. Vendor will control. "
        "See Section 6.4 for details; 6.4 Account Lifecycle. Vendor will revoke."
    )
    lattice = EnumeratorLattice.from_elements([welded])
    positions = lattice.positions(welded.text)
    starts = [welded.text[p : p + 3] for p in positions]
    assert starts == ["6.3", "6.4"]
    # the cross-reference "Section 6.4" is not a position
    assert welded.text.index("6.4 for") not in positions


def test_split_welded_separates_clauses():
    welded = para("6.2 MFA. Vendor will enforce MFA. 6.3 PAM. Vendor will control.", y=100)
    lattice = EnumeratorLattice.from_elements([welded])
    pieces = split_welded([welded], lattice)
    assert [p.text for p in pieces] == [
        "6.2 MFA. Vendor will enforce MFA.",
        "6.3 PAM. Vendor will control.",
    ]
    assert all(p.page_index == 0 and p.type == "paragraph" for p in pieces)
    assert pieces[0].bbox[1] < pieces[1].bbox[1] <= pieces[1].bbox[3]


def test_wrapped_lines_still_join_under_the_veto():
    profile = make_profile()
    lines = [
        para("6.2 MFA. Vendor will enforce multi-factor authentication for", y=100),
        para("all privileged accounts and production access.", y=114, x1=400),
    ]
    lattice = EnumeratorLattice.from_elements(lines + [para("6.3 PAM. Text.", y=140)])
    merged = join_wrapped_lines(lines, profile, lattice=lattice)
    assert len(merged) == 1
    assert merged[0].text.endswith("for all privileged accounts and production access.")


def test_corroborated_enumerator_vetoes_the_merge():
    profile = make_profile()
    lines = [
        para("6.1 Least Privilege. Vendor will implement least-privilege access to the", y=100),
        para("6.2 MFA. Vendor will enforce MFA for privileged access and production.", y=114),
        para("6.3 PAM. Vendor will control privileged access through a bastion.", y=128, x1=400),
    ]
    lattice = EnumeratorLattice.from_elements(lines)
    assert len(join_wrapped_lines(lines, profile)) == 1  # geometry alone welds them
    assert len(join_wrapped_lines(lines, profile, lattice=lattice)) == 3


def test_indented_first_line_only_continues_when_the_document_indents():
    lines = [
        para("A first line that is indented and runs to the right margin of", y=100, x0=54),
        para("the page, then continues flush left.", y=114, x1=400),
    ]
    assert len(join_wrapped_lines(lines, make_profile(paragraph_indent=0.0))) == 2
    assert len(join_wrapped_lines(lines, make_profile(paragraph_indent=18.0))) == 1


# --------------------------------------------------------------------------
# P5 -- page spans


def test_merge_across_a_page_break_records_the_span():
    profile = make_profile()
    lines = [
        para("Vendor will notify Company of legally binding requests and honour", page=8, y=743),
        para("Company legal holds to the extent lawful.", page=9, y=36, x1=240),
        para("A separate paragraph.", page=9, y=60, x1=200),
    ]
    merged = join_wrapped_lines(lines, profile)
    assert len(merged) == 2
    assert (merged[0].page_index, merged[0].page_end) == (8, 9)
    assert (merged[0].page_label, merged[0].page_label_end) == ("9", "10")
    assert merged[0].page_span == (8, 9)
    assert merged[1].page_end is None and merged[1].page_span == (9, 9)


def test_chunk_carries_the_span():
    from contract_analyzer.models import Chunk

    chunk = Chunk(ordinal=0, content="x", page=3, page_label="4", page_end=4, page_label_end="5")
    assert (chunk.page_end, chunk.page_label_end) == (4, "5")


# --------------------------------------------------------------------------
# P1 -- section spine


def _spine_fixture():
    elements = [
        heading("Information Security Addendum", y=10),
        para("This Addendum is between the parties.", y=30),
        heading("1. Scope", y=50),
        para("1.1 Applicability. This Addendum applies to Vendor.", y=70),
        para("1.2 “Company Data” means all data supplied by Company under the Agreement.", y=90),
        para("Following text under 1.2.", y=110),
        heading("2. Definitions", y=130),
        para("SAML 2.0 is mentioned here.", y=150),
        heading("Exhibit G — Security Schedule", y=170),
        heading("G1. Governance", y=190),
        heading("G2. Assets", y=210),
        para("Text under G2.", y=230),
        heading("Signatures", y=250),
    ]
    return elements, EnumeratorLattice.from_elements(elements)


def test_synthesized_spine_nests_by_enumerator():
    elements, lattice = _spine_fixture()
    spine = synthesize_spine(elements, lattice)
    assert [(s.level, s.title) for s in spine] == [
        (1, "Information Security Addendum"),
        (1, "1. Scope"),
        (2, "1.1 Applicability"),
        (2, "1.2 “Company Data”"),
        (1, "2. Definitions"),
        (1, "Exhibit G — Security Schedule"),
        (2, "G1. Governance"),
        (2, "G2. Assets"),
        (1, "Signatures"),
    ]
    by_title = {s.title: s for s in spine}
    assert by_title["1.1 Applicability"].path == ["1. Scope", "1.1 Applicability"]
    assert by_title["G2. Assets"].path == ["Exhibit G — Security Schedule", "G2. Assets"]
    assert all(s.start_y is not None for s in spine)


def test_synthesized_spine_assigns_sections():
    elements, lattice = _spine_fixture()
    spine = synthesize_spine(elements, lattice)
    assign_sections(elements, spine)
    paths = {e.text[:12]: e.section_path for e in elements}
    assert paths["This Addendu"] == ["Information Security Addendum"]
    assert paths["1.2 “Company"] == ["1. Scope", "1.2 “Company Data”"]
    assert paths["Following te"] == ["1. Scope", "1.2 “Company Data”"]
    assert paths["SAML 2.0 is "] == ["2. Definitions"]
    assert paths["Text under G"] == ["Exhibit G — Security Schedule", "G2. Assets"]


def test_uncorroborated_paragraph_number_does_not_become_a_section():
    elements = [heading("1. Scope", y=10), para("2.0 is a version, not a clause.", y=30)]
    spine = synthesize_spine(elements, EnumeratorLattice.from_elements(elements))
    assert [s.title for s in spine] == ["1. Scope"]


def test_empty_stream_gives_empty_spine():
    assert synthesize_spine([], EnumeratorLattice.from_elements([])) == []


# --------------------------------------------------------------------------
# the sample contract


CLAUSES = (
    [f"2.{i}" for i in range(1, 5)]
    + [f"3.{i}" for i in range(1, 10)]
    + [f"4.{i}" for i in range(1, 6)]
    + [f"5.{i}" for i in range(1, 4)]
    + [f"6.{i}" for i in range(1, 8)]
    + [f"7.{i}" for i in range(1, 5)]
    + [f"8.{i}" for i in range(1, 4)]
    + [f"9.{i}" for i in range(1, 4)]
    + [f"12.{i}" for i in range(1, 5)]
    + [f"13.{i}" for i in range(1, 8)]
)


def _alnum(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


@needs_sample
def test_sample_text_is_conserved_exactly(sample):
    import pymupdf

    with pymupdf.open(SAMPLE_PDF) as doc:
        on_page = sum(_alnum(page.get_text()) for page in doc)
    extracted = sum(_alnum(e.text) for e in sample.elements + sample.furniture)
    # The one permitted deletion: the repeated header row of a table stitched
    # across a page break. It is accounted for, not assumed away.
    dropped_headers = sum(
        _alnum(" ".join(t.rows[0])) * (t.page_end - t.page_index)
        for t in sample.tables
        if t.page_end is not None and t.rows
    )
    assert extracted + dropped_headers == on_page


@needs_sample
def test_sample_stream_is_well_formed(sample):
    elements = sample.elements
    assert [e.order for e in elements] == list(range(len(elements)))
    assert all(e.text.strip() for e in elements)
    pages = [e.page_index for e in elements]
    assert pages == sorted(pages)
    assert sample.furniture == []
    assert sample.profile.footer_band > 0.9  # measured from where body text stops


@needs_sample
def test_sample_spine_is_synthesized_and_complete(sample):
    assert not sample.has_outline
    assert sample.spine_source == "headings"
    assert all(e.section_path for e in sample.elements)
    assert len(sample.of_type("heading")) == 51


@needs_sample
def test_sample_clauses_are_standalone_elements(sample):
    paragraphs = sample.of_type("paragraph")
    for label in CLAUSES:
        starters = [p for p in paragraphs if p.text.startswith(f"{label} ")]
        assert len(starters) == 1, label
        buried_re = re.compile(rf"[.;:”)]\s+{re.escape(label)}\s+[A-Z“]")
        buried = [p for p in paragraphs if buried_re.search(p.text)]
        assert buried == [], label
    assert max(len(p.text) for p in paragraphs) < 4000


@needs_sample
def test_sample_compliance_clauses_carry_breadcrumbs(sample):
    by_prefix = {p.text[:4].strip(): p for p in sample.of_type("paragraph")}
    assert by_prefix["6.6"].section_path == [
        "6. Identity, Access, Authentication, and Password Management",
        "6.6 Password Management Standard",
    ]
    assert by_prefix["7.2"].section_path == [
        "7. Encryption and Key Management",
        "7.2 Data in Transit Requirements (TLS)",
    ]
    assert by_prefix["9.1"].section_path[0] == "9. IT Asset Management and Secure Configuration"
    assert by_prefix["3.1"].section_path[1] == "3.1 “Company Data”"


@needs_sample
def test_sample_exhibit_subsections_nest_under_their_exhibit(sample):
    g3a = next(e for e in sample.elements if e.text.startswith("G3A."))
    assert g3a.section_path == [
        "Exhibit G — Security Schedule (Numbered Requirements)",
        "G3A. Password Management (Added)",
    ]
    tables_under_g = [t for t in sample.tables if t.section_path[0].startswith("Exhibit G")]
    assert {t.section_path[-1][:4].rstrip(". ") for t in tables_under_g} >= {
        f"G{i}" for i in range(1, 14)
    } | {"G3A"}


@needs_sample
def test_sample_prose_hyphens_are_lexical(sample):
    prose = " ".join(p.text for p in sample.of_type("paragraph"))
    assert "just-in-time" in prose
    assert "just-intime" not in prose
    assert sample.profile.breaks_hyphenate is False


@needs_sample
def test_sample_cross_page_paragraph_carries_page_end(sample):
    holds = next(p for p in sample.of_type("paragraph") if "legally binding requests" in p.text)
    assert (holds.page_index, holds.page_end) == (8, 9)
    assert holds.page_label_end == "10"
    assert "Company legal holds to the extent lawful." in holds.text


@needs_sample
def test_sample_parses_quickly():
    from contract_analyzer.parse import parse_pdf

    start = time.perf_counter()
    parse_pdf(SAMPLE_PDF, extract_figures=False)
    assert time.perf_counter() - start < 2.0


@needs_sample
def test_sample_tables_are_all_valid(sample):
    from contract_analyzer.parse.tables import validate

    assert all(isinstance(t, TableElement) and validate(t.rows) for t in sample.tables)
