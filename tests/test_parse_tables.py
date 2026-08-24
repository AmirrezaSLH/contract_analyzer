"""Parser regression suite: table cells and tables that span a page break."""

from __future__ import annotations

import re

from conftest import make_profile, needs_sample
from contract_analyzer.parse.elements import Element, TableElement
from contract_analyzer.parse.pdf import stitch_spanning_tables
from contract_analyzer.parse.tables import compact, rows_to_markdown, validate

PAGE_HEIGHT = 792.0

# --------------------------------------------------------------------------
# P3 -- cells go through the same line joiner as prose


def test_compact_joins_wrapped_cell_lines_with_the_profile():
    profile = make_profile(words=["maintain", "a", "written", "program", "alerting", "monitoring"])
    rows = [
        ["ID", "Requirement", "Monitoring/Alertin\ng"],
        ["GOV-\n01", "Maintain a written\nprogram", None],
    ]
    assert compact(rows, profile) == [
        ["ID", "Requirement", "Monitoring/Alerting"],
        ["GOV-01", "Maintain a written program", ""],
    ]


def test_compact_keeps_lexical_hyphens_in_cells():
    profile = make_profile(hyphenated=["in-scope"], breaks_hyphenate=False)
    rows = [["Included In-\nScope?", "in-\nscope assets"], ["N/A (token-\nbased)", "x"]]
    assert compact(rows, profile) == [
        ["Included In-Scope?", "in-scope assets"],
        ["N/A (token-based)", "x"],
    ]


def test_compact_still_drops_empty_rows_and_columns():
    rows = [[None, "a", None], [None, None, None], ["", "b", ""]]
    assert compact(rows) == [["a"], ["b"]]
    assert compact(rows, make_profile()) == [["a"], ["b"]]


# --------------------------------------------------------------------------
# P4 -- page-spanning tables


def _table(page: int, y0: float, y1: float, rows: list[list[str]]) -> TableElement:
    return TableElement(
        text=rows_to_markdown(rows),
        markdown=rows_to_markdown(rows),
        rows=rows,
        page_index=page,
        page_label=str(page + 1),
        bbox=(36.0, y0, 577.0, y1),
        quality="ruled",
    )


HEADER = ["Control Domain", "Control Requirement", "Minimum Standard"]
A_ROWS = [HEADER, ["Governance", "Program", "ISO 27001"], ["Access", "MFA", "All admins"]]
B_ROWS = [HEADER, ["Crypto", "TLS", "1.2+"]]


def _heights(*pages: int) -> dict[int, float]:
    return dict.fromkeys(pages, PAGE_HEIGHT)


def test_continuation_on_the_next_page_is_stitched():
    a = _table(9, 346.0, 752.0, A_ROWS)
    b = _table(10, 36.0, 286.0, B_ROWS)
    out = stitch_spanning_tables([a, b], _heights(9, 10))
    assert len(out) == 1
    merged = out[0]
    assert merged.rows == A_ROWS + B_ROWS[1:]  # the repeated header is dropped once
    assert merged.markdown == rows_to_markdown(merged.rows)
    assert merged.text == merged.markdown
    assert (merged.page_index, merged.page_end) == (9, 10)
    assert (merged.page_label, merged.page_label_end) == ("10", "11")
    assert merged.bbox == a.bbox
    assert validate(merged.rows)


def test_stitching_needs_the_next_page_and_no_element_between():
    a = _table(9, 346.0, 752.0, A_ROWS)
    b = _table(11, 36.0, 286.0, B_ROWS)
    assert len(stitch_spanning_tables([a, b], _heights(9, 10, 11))) == 2

    a = _table(9, 346.0, 752.0, A_ROWS)
    g = Element(
        type="heading", text="G2. Assets", page_index=10, page_label="11", bbox=(36, 20, 200, 34)
    )
    b = _table(10, 40.0, 286.0, B_ROWS)
    assert len(stitch_spanning_tables([a, g, b], _heights(9, 10))) == 3


def test_stitching_needs_an_identical_header():
    a = _table(9, 346.0, 752.0, A_ROWS)
    b = _table(10, 36.0, 286.0, [["ID", "Requirement", "Standard"], ["x", "y", "z"]])
    assert len(stitch_spanning_tables([a, b], _heights(9, 10))) == 2
    # a different column count with a matching prefix is not a match either
    c = _table(10, 36.0, 286.0, [HEADER + ["Notes"], ["x", "y", "z", "w"]])
    assert len(stitch_spanning_tables([a, c], _heights(9, 10))) == 2


def test_stitching_needs_a_bottom_then_top_geometry():
    # A ends high on its page: whatever follows on the next page is a new table.
    a = _table(9, 100.0, 300.0, A_ROWS)
    b = _table(10, 36.0, 286.0, B_ROWS)
    assert len(stitch_spanning_tables([a, b], _heights(9, 10))) == 2
    # B starts low on its page: it did not resume at the top.
    a = _table(9, 346.0, 752.0, A_ROWS)
    b = _table(10, 500.0, 700.0, B_ROWS)
    assert len(stitch_spanning_tables([a, b], _heights(9, 10))) == 2


def test_a_three_page_table_stitches_transitively():
    a = _table(0, 400.0, 760.0, A_ROWS)
    b = _table(1, 36.0, 760.0, B_ROWS)
    c = _table(2, 36.0, 200.0, [HEADER, ["Net", "Segmentation", "Zones"]])
    out = stitch_spanning_tables([a, b, c], _heights(0, 1, 2))
    assert len(out) == 1
    assert out[0].rows == A_ROWS + B_ROWS[1:] + [["Net", "Segmentation", "Zones"]]
    assert (out[0].page_index, out[0].page_end) == (0, 2)


# --------------------------------------------------------------------------
# the sample contract

CONTROL_IDS = {
    "GOV-01", "GOV-02", "GOV-03", "GOV-04", "GOV-05",
    "ASSET-01", "ASSET-02", "ASSET-03",
    "IAM-01", "IAM-02", "IAM-03", "IAM-04", "IAM-05", "IAM-06",
    "PASS-01", "PASS-02", "PASS-03", "PASS-04",
    "CRYP-01", "CRYP-02", "CRYP-03", "CRYP-04", "SECR-01",
    "VULN-01", "VULN-02", "VULN-03", "VULN-04",
    "LOG-01", "LOG-02", "LOG-03", "LOG-04", "REC-01", "REC-02",
    "BCDR-01", "BCDR-02", "BCDR-03", "SUP-01", "SUP-02", "SUP-03",
    "LOC-01", "LOC-02", "LOC-03", "NET-01", "NET-02", "NET-03",
}  # fmt: skip


def _cells(sample) -> list[str]:
    return [cell for t in sample.tables for row in t.rows for cell in row]


@needs_sample
def test_sample_control_ids_are_intact(sample):
    cells = _cells(sample)
    corrupted = [c for c in cells if re.search(r"[A-Z][A-Z0-9]*-\s+\w", c)]
    assert corrupted == []
    found = {c for c in cells if re.fullmatch(r"[A-Z]{2,5}-\d{2}", c)}
    assert found >= CONTROL_IDS


@needs_sample
def test_sample_wrapped_cells_are_repaired(sample):
    cells = " | ".join(_cells(sample))
    for expected in (
        "Monitoring/Alerting",
        "Requirement Ref",
        "Included In-Scope?",
        "in-scope assets",
        "token-based",
        "out-of-region",
        "semi-annual",
        "break-glass",
    ):
        assert expected in cells, expected
    for corrupted in ("Alertin g", "Requiremen t", "tokenbased", "ofregion", "Acceptanc e"):
        assert corrupted not in cells, corrupted


@needs_sample
def test_sample_page_spanning_tables_are_stitched(sample):
    tables = sample.tables
    assert len(tables) == 34
    spanning = [t for t in tables if t.page_end is not None]
    assert len(spanning) == 8
    assert all(t.page_end == t.page_index + 1 for t in spanning)
    matrix = next(t for t in tables if t.rows[0][:2] == ["Control Domain", "Control Requirement"])
    assert (matrix.page_index, matrix.page_end, matrix.n_rows) == (9, 10, 20)
    assert matrix.section_path[0].startswith("Exhibit A")


@needs_sample
def test_sample_exhibit_g_keeps_one_table_per_subsection(sample):
    under_g = [t for t in sample.tables if t.section_path[0].startswith("Exhibit G")]
    # 16 requirement tables on the page, two of which continue over a break.
    assert len(under_g) == 14
    # Every G subsection still owns its own table; none were merged together.
    assert len({t.section_path[-1] for t in under_g}) == 14


@needs_sample
def test_sample_grids_pass_validation(sample):
    for table in sample.tables:
        assert table.quality == "ruled"
        assert validate(table.rows), table.rows[0]
        assert table.markdown == rows_to_markdown(table.rows)
