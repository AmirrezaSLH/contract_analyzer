"""The two tools the agent retrieves with, and the ledger their results go in.

Thin wrappers over `retrieval`; nothing new happens in retrieval itself. The
model chooses the query, the mode and the depth. It does **not** choose the
contract: `document_id` is bound here, on the Python side, so the step-9
scope guarantee holds one layer up -- no tool call, however phrased, can
reach another contract's text.

**Evidence ledger.** Every chunk any tool returns is registered once in a
per-run `Evidence` and given a stable id, `E1`, `E2`, ... A tool result
renders the *new* chunks in full and the already-seen ones as ids only, so a
second search that overlaps the first costs the model a line, not a page.
The ledger is what the analysis finisher verifies quotes against, what the
chat finisher's document blocks are built from, and what the log records.

**Not getting stuck.** Two of the loop's three caps live here because they
are about evidence, not turns: an identical `(tool, args)` returns the ids it
already produced at zero retrieval cost, and once the ledger's token total
reaches `max_evidence_tokens` a tool result says so instead of retrieving.
The turn cap is the loop's, in `agent.py`.

**Offline.** `mode="keyword"` needs no embedder. Asking for `vector` or
`hybrid` without one returns a tool *result* that says to use keyword, not
an exception: a run on a machine with no embedding key degrades to BM25
rather than dying.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..embeddings.base import Embedder
from ..logger import get_logger, span
from ..retrieval import NEEDS_EMBEDDER, RetrievedChunk, retrieve, retrieve_by_section
from ..tokens import count_tokens

log = get_logger(__name__)

MODES = ("hybrid", "vector", "keyword")
MIN_TOP_K, MAX_TOP_K = 1, 12
#: Chunks a `get_section` call may return. A whole exhibit fits; a runaway
#: prefix ("1") does not get the whole contract.
SECTION_LIMIT = 12

SEARCH_TOOL = "search_contract"
SECTION_TOOL = "get_section"


@dataclass(frozen=True)
class EvidenceEntry:
    """One ledger line: the id the model sees, the chunk it names."""

    id: str
    chunk: RetrievedChunk
    tokens: int

    @property
    def title(self) -> str:
        """`6. Identity > 6.6 Password Management Standard (p.9-10)` -- the full path."""
        crumb = self.chunk.breadcrumb or self.chunk.section
        page = self.chunk.page_display
        if crumb and page:
            return f"{crumb} (p.{page})"
        return crumb or (f"p.{page}" if page else self.chunk.filename)

    def render(self) -> str:
        return f"[{self.id}] {self.title}\n{self.chunk.text_for_model()}"


class Evidence:
    """The chunks one run has retrieved, in the order they first appeared."""

    def __init__(self) -> None:
        self._entries: list[EvidenceEntry] = []
        self._by_chunk: dict[int, str] = {}
        self._by_id: dict[str, EvidenceEntry] = {}
        self.tokens = 0

    def register(self, chunks: Sequence[RetrievedChunk]) -> tuple[list[str], list[str]]:
        """Add what is new. Returns `(new_ids, seen_ids)` in result order."""
        new: list[str] = []
        seen: list[str] = []
        for chunk in chunks:
            existing = self._by_chunk.get(chunk.chunk_id)
            if existing is not None:
                seen.append(existing)
                continue
            eid = f"E{len(self._entries) + 1}"
            entry = EvidenceEntry(id=eid, chunk=chunk, tokens=count_tokens(chunk.text_for_model()))
            self._entries.append(entry)
            self._by_chunk[chunk.chunk_id] = eid
            self._by_id[eid] = entry
            self.tokens += entry.tokens
            new.append(eid)
        return new, seen

    def get(self, eid: str) -> EvidenceEntry:
        return self._by_id[eid]

    def __contains__(self, eid: object) -> bool:
        return eid in self._by_id

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[EvidenceEntry]:
        return iter(self._entries)

    @property
    def ids(self) -> list[str]:
        return [entry.id for entry in self._entries]

    @property
    def chunks(self) -> list[RetrievedChunk]:
        return [entry.chunk for entry in self._entries]


@dataclass
class ToolCall:
    """What one tool execution did -- the log line and the KPI row."""

    name: str
    args: dict[str, Any]
    mode: str | None = None
    top_k: int | None = None
    #: Chunks the call returned, and how many of them were new to the ledger.
    returned: int = 0
    new: int = 0
    ids: list[str] = field(default_factory=list)
    #: False when the call was answered from the ledger or refused by a cap.
    retrieved: bool = False
    #: Why nothing was retrieved when nothing was wrong: `duplicate`, `budget`.
    note: str | None = None
    #: A bad input. The model gets it back as an error result.
    error: str | None = None


@dataclass
class ToolOutcome:
    """The text handed back to the model, and the record kept of it."""

    text: str
    call: ToolCall


class ContractTools:
    """`search_contract` and `get_section`, bound to one contract, one ledger."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        document_id: int,
        embedder: Embedder | None = None,
        settings: Settings | None = None,
        max_evidence_tokens: int | None = None,
    ) -> None:
        self._conn = conn
        self._document_id = int(document_id)
        self._embedder = embedder
        self._settings = settings or get_settings()
        self.max_evidence_tokens = max_evidence_tokens or self._settings.max_evidence_tokens
        self.evidence = Evidence()
        self.calls: list[ToolCall] = []
        self._seen: dict[tuple[str, str], list[str]] = {}

    # -- definitions --------------------------------------------------------

    def definitions(self) -> list[dict[str, Any]]:
        """The tool list for the API. `document_id` is deliberately absent."""
        default_k = min(MAX_TOP_K, max(MIN_TOP_K, self._settings.retrieval_top_k))
        offline = "" if self._embedder is not None else (
            " No embedder is configured in this run, so only `keyword` works;"
            " `vector` and `hybrid` will be refused."
        )
        return [
            {
                "name": SEARCH_TOOL,
                "description": (
                    "Search this contract for passages matching a query. Choose the "
                    "mode: `keyword` (BM25) wins for identifiers and exact jargon such "
                    "as GOV-01, TLS 1.2 or SAML; `vector` wins for paraphrase, where "
                    "the contract's wording differs from yours ('secure admin pathway' "
                    "for 'bastion'); `hybrid` fuses both and is the default when "
                    "unsure. Results are labelled with evidence ids (E1, E2, ...); a "
                    "passage already retrieved is listed by id only." + offline
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for."},
                        "mode": {
                            "type": "string",
                            "enum": list(MODES),
                            "description": "hybrid (default), vector or keyword.",
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": MIN_TOP_K,
                            "maximum": MAX_TOP_K,
                            "description": f"Passages to return, {MIN_TOP_K}-{MAX_TOP_K} "
                            f"(default {default_k}).",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": SECTION_TOOL,
                "description": (
                    "Return a clause or exhibit of this contract by its number or label, "
                    "in document order: '6.6', '6.6.2', 'Exhibit G'. Use it when a "
                    "passage you already have names the section to read next. The "
                    "prefix matches the start of a section title, so '6.6' returns "
                    "6.6 and its sub-clauses but not 16.6."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": "A clause number or exhibit label.",
                        }
                    },
                    "required": ["prefix"],
                },
            },
        ]

    @property
    def names(self) -> frozenset[str]:
        return frozenset({SEARCH_TOOL, SECTION_TOOL})

    @property
    def budget_reached(self) -> bool:
        return self.evidence.tokens >= self.max_evidence_tokens

    # -- execution ----------------------------------------------------------

    def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool call. Never raises for a bad input: the model gets a result."""
        call = ToolCall(name=name, args=dict(args))
        with span("agent.tool", log, tool=name) as bag:
            if name == SEARCH_TOOL:
                text = self._search(call)
            elif name == SECTION_TOOL:
                text = self._section(call)
            else:
                call.error = f"unknown tool {name!r}"
                text = f"Error: {call.error}. Available: {SEARCH_TOOL}, {SECTION_TOOL}."
            bag.update(
                mode=call.mode,
                top_k=call.top_k,
                returned=call.returned,
                new=call.new,
                retrieved=call.retrieved,
                evidence_tokens=self.evidence.tokens,
            )
            if call.note:
                bag["note"] = call.note
            if call.error:
                bag["error"] = call.error
        self.calls.append(call)
        return ToolOutcome(text=text, call=call)

    def _search(self, call: ToolCall) -> str:
        query = str(call.args.get("query", "")).strip()
        mode = call.args.get("mode") or self._settings.retrieval_mode
        top_k = call.args.get("top_k")
        call.mode, call.top_k = mode, top_k
        if not query:
            call.error = "query is empty"
            return "Error: `query` must not be empty."
        if mode not in MODES:
            call.error = f"unknown mode {mode!r}"
            return f"Error: mode must be one of {', '.join(MODES)}."
        if top_k is None:
            top_k = self._settings.retrieval_top_k
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            call.error = f"top_k {top_k!r} is not an integer"
            return f"Error: top_k must be an integer between {MIN_TOP_K} and {MAX_TOP_K}."
        top_k = min(MAX_TOP_K, max(MIN_TOP_K, top_k))
        call.top_k = top_k
        if mode in NEEDS_EMBEDDER and self._embedder is None:
            call.error = "no embedder configured"
            return (
                f"Error: mode={mode!r} needs an embedder and none is configured in this "
                "run; call again with mode='keyword'."
            )
        return self._deliver(call, lambda: self._retrieve(query, mode, top_k))

    def _retrieve(self, query: str, mode: str, top_k: int) -> list[RetrievedChunk]:
        result = retrieve(
            query,
            self._conn,
            self._embedder,
            self._settings,
            document_id=self._document_id,
            mode=mode,
            top_k=top_k,
        )
        return list(result.chunks)

    def _section(self, call: ToolCall) -> str:
        prefix = str(call.args.get("prefix", "")).strip()
        if not prefix:
            call.error = "prefix is empty"
            return "Error: `prefix` must not be empty."
        return self._deliver(
            call,
            lambda: retrieve_by_section(
                self._conn, self._document_id, prefix, limit=SECTION_LIMIT
            ),
        )

    def _deliver(self, call: ToolCall, fetch) -> str:
        """The shared tail: dedupe, budget, retrieve, register, render."""
        key = (call.name, json.dumps(call.args, sort_keys=True, default=str))
        if key in self._seen:
            ids = self._seen[key]
            call.ids, call.returned, call.note = list(ids), len(ids), "duplicate"
            if not ids:
                return "No passages matched (same call as before)."
            return f"Same call as before; already retrieved: {', '.join(ids)}"
        if self.budget_reached:
            call.note = "budget"
            return (
                f"Evidence budget reached ({self.evidence.tokens} tokens over "
                f"{len(self.evidence)} passages). Do not search again; answer from "
                "the passages you have."
            )
        chunks = fetch()
        call.retrieved = True
        new, seen = self.evidence.register(chunks)
        ids = [self.evidence._by_chunk[c.chunk_id] for c in chunks]
        self._seen[key] = ids
        call.ids, call.returned, call.new = ids, len(chunks), len(new)
        if not chunks:
            return "No passages matched. Try other words, another mode, or a section number."
        parts = [self.evidence.get(eid).render() for eid in new]
        if seen:
            parts.append(f"already retrieved: {', '.join(seen)}")
        if self.budget_reached:
            parts.append(
                f"Evidence budget reached ({self.evidence.tokens} tokens). "
                "Do not search again; answer from the passages you have."
            )
        return "\n\n".join(parts)


__all__ = [
    "MAX_TOP_K",
    "MIN_TOP_K",
    "MODES",
    "SEARCH_TOOL",
    "SECTION_TOOL",
    "ContractTools",
    "Evidence",
    "EvidenceEntry",
    "ToolCall",
    "ToolOutcome",
]
