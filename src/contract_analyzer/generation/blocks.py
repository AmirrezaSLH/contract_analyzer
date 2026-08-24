"""Document blocks for the chat finisher, and the citations that come back.

One block per ledger entry, in `E` order, citations enabled on all of them
(the API wants all or none). The source is **plain text**, so the citations
return as `char_location`: `cited_text` is extracted by the API from the
bytes we sent -- it cannot be invented -- and the offsets index the exact
passage, which is what a highlight in the UI needs. Not the `content`
source: its citations are `content_block_location`, block indexes, coarser,
useless for highlighting.

A block's `title` is the **full** breadcrumb plus the printed page range,
where step 9's `citation_title` is the leaf; different on purpose, because
the full path is what separates `G1. Governance` from `6.1 Governance` in a
contract that has both, and a document title has no width to fit. `context`
carries the filename, the element type, and `sections inferred` when the
breadcrumb was synthesised rather than read from an outline -- a reviewer
checking a citation deserves to know.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tools import Evidence, EvidenceEntry


@dataclass(frozen=True)
class Citation:
    """One API citation, resolved to the passage it points into."""

    entry: EvidenceEntry
    quote: str
    start: int
    end: int
    document_index: int

    @property
    def evidence_id(self) -> str:
        return self.entry.id

    @property
    def title(self) -> str:
        return self.entry.title

    @property
    def page_display(self) -> str:
        return self.entry.chunk.page_display


def document_block(entry: EvidenceEntry) -> dict[str, Any]:
    chunk = entry.chunk
    context = [chunk.filename, chunk.element_type]
    if chunk.spine_source != "outline":
        context.append("sections inferred")
    return {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": chunk.text_for_model()},
        "title": entry.title,
        "context": ", ".join(context),
        "citations": {"enabled": True},
    }


def document_blocks(evidence: Evidence) -> list[dict[str, Any]]:
    """One block per ledger entry, in `E` order; `document_index` i is `E{i+1}`."""
    return [document_block(entry) for entry in evidence]


def resolve_citations(message: Any, evidence: Evidence) -> list[Citation]:
    """Every citation on the message, in reading order, resolved to its passage.

    An out-of-range `document_index` is dropped, not raised: a citation the
    UI cannot show is worth a log line, not a failed answer.
    """
    entries = list(evidence)
    citations: list[Citation] = []
    for block in message.content:
        if block.type != "text":
            continue
        for citation in getattr(block, "citations", None) or []:
            index = getattr(citation, "document_index", None)
            if index is None or not 0 <= index < len(entries):
                continue
            citations.append(
                Citation(
                    entry=entries[index],
                    quote=getattr(citation, "cited_text", "") or "",
                    start=getattr(citation, "start_char_index", 0) or 0,
                    end=getattr(citation, "end_char_index", 0) or 0,
                    document_index=index,
                )
            )
    return citations


def answer_text(message: Any) -> str:
    return "".join(block.text for block in message.content if block.type == "text")


__all__ = ["Citation", "answer_text", "document_block", "document_blocks", "resolve_citations"]
