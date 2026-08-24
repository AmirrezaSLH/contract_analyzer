"""Shared fixtures.

The sample contract is not committed (it lands with the fixture commit), so
every test that reads it is skipped when the file is absent. Parsing takes
about a second, so it is done once per session.
"""

from __future__ import annotations

from collections import Counter
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
