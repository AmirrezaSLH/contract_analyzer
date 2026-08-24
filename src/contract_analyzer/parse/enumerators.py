"""Enumerators: the labels that open numbered clauses, corroborated by sequence.

A regex can say that a string *looks like* a clause number. Whether it *is*
one is decided by whether it takes its place in a sequence with its siblings:
``6.3`` that follows ``6.2`` is a clause, ``2.0`` in "SAML 2.0" belongs to no
sequence and is not. That distinction is what makes it safe to split text on
enumerators and to veto a paragraph merge when the next line opens one. The
sample contract has 29 decimal numbers in mid-prose and 41 real clause labels,
and the sequence test separates them with no per-document tuning.

The shapes cover the ways legal and technical documents number things, not
just the one in front of us:

=============  =====================  ==========================
kind           example                sequence
=============  =====================  ==========================
``integer``    ``21.``                20 -> 21
``decimal``    ``6.6``, ``12.4.1``    6.5 -> 6.6 (within 6)
``alnum``      ``G3A.``               G3 -> G3A -> G4 (within G)
``exhibit``    ``Exhibit G``          Exhibit F -> Exhibit G
``lettered``   ``(a)``                (a) -> (b), restarting freely
``roman``      ``(iv)``               (iii) -> (iv), restarting freely
=============  =====================  ==========================

The first four are *sectional*: they can head a section, split a welded
element, and take a place in the spine. The last two only stop a merge.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .elements import Element

#: What may follow an enumerator for it to be opening a clause: a capital, an
#: opening quote (a defined term) or an opening parenthesis. A lowercase word
#: means the number was part of a sentence -- "1.2 is the minimum version".
_TITLE_START = r'(?=[A-Z“"(\[])'

_EXHIBIT = re.compile(r"(Exhibit|Schedule|Annex|Appendix|Attachment)\s+([A-Z]|\d{1,2})\b")
_DECIMAL = re.compile(r"(\d{1,3}(?:\.\d{1,3})+)\.?\s+" + _TITLE_START)
_INTEGER = re.compile(r"(\d{1,3})\.\s+" + _TITLE_START)
_ALNUM = re.compile(r"([A-Z]{1,3})(\d{1,3})([A-Z]?)\.\s+" + _TITLE_START)
_ROMAN = re.compile(r"\(([ivxlcdm]{2,})\)\s+")
_LETTERED = re.compile(r"\(([a-z])\)\s+")

#: A sentence terminator followed by space: the only place an enumerator may
#: sit in the middle of an element's text. "for (a) privileged" and
#: "Section 6.4 for details" fail this and are never candidates.
_AFTER_TERMINATOR = re.compile(r"""[.;:!?”"’')\]]\s+""")

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

SECTIONAL = frozenset({"integer", "decimal", "alnum", "exhibit"})


def _roman_to_int(text: str) -> int | None:
    total = 0
    prev = 0
    for ch in reversed(text.lower()):
        value = _ROMAN_VALUES.get(ch)
        if value is None:
            return None
        total += value if value >= prev else -value
        prev = max(prev, value)
    return total


@dataclass(frozen=True)
class Enumerator:
    """One clause label, parsed."""

    label: str  # as printed, less the trailing separator: "6.6", "G3A", "Exhibit G"
    kind: str
    #: What a child refers to as its parent: "6" for "6.6", "G" for "Exhibit G".
    key: str
    #: The key of the enumerator this one nests under; "" at the top level.
    parent: str
    #: Position within the parent's sequence; comparable between siblings.
    ordinal: tuple[int, ...]
    #: Character offset just past the label and its separator.
    end: int

    @property
    def depth(self) -> int:
        if self.kind == "decimal":
            return self.label.count(".") + 1
        if self.kind == "alnum":
            return 2
        return 1 if self.kind in ("integer", "exhibit") else 0

    @property
    def sectional(self) -> bool:
        return self.kind in SECTIONAL


def match_enumerator(text: str, pos: int = 0) -> Enumerator | None:
    """The enumerator opening `text` at `pos`, or None if nothing does."""
    m = _EXHIBIT.match(text, pos)
    if m:
        ident = m.group(2)
        ordinal = (int(ident),) if ident.isdigit() else (ord(ident) - ord("A") + 1,)
        return Enumerator(
            label=f"{m.group(1)} {ident}", kind="exhibit", key=ident, parent="",
            ordinal=ordinal, end=m.end(),
        )  # fmt: skip
    m = _DECIMAL.match(text, pos)
    if m:
        parts = m.group(1).split(".")
        return Enumerator(
            label=m.group(1), kind="decimal", key=m.group(1), parent=".".join(parts[:-1]),
            ordinal=(int(parts[-1]),), end=m.end(),
        )  # fmt: skip
    m = _INTEGER.match(text, pos)
    if m:
        return Enumerator(
            label=m.group(1), kind="integer", key=m.group(1), parent="",
            ordinal=(int(m.group(1)),), end=m.end(),
        )  # fmt: skip
    m = _ALNUM.match(text, pos)
    if m:
        letters, number, suffix = m.groups()
        return Enumerator(
            label=f"{letters}{number}{suffix}", kind="alnum", key=f"{letters}{number}{suffix}",
            parent=letters, ordinal=(int(number), ord(suffix) - ord("A") + 1 if suffix else 0),
            end=m.end(),
        )  # fmt: skip
    m = _ROMAN.match(text, pos)
    if m:
        value = _roman_to_int(m.group(1))
        if value is not None:
            return Enumerator(
                label=f"({m.group(1)})", kind="roman", key=f"({m.group(1)})", parent="(roman)",
                ordinal=(value,), end=m.end(),
            )  # fmt: skip
    m = _LETTERED.match(text, pos)
    if m:
        return Enumerator(
            label=f"({m.group(1)})", kind="lettered", key=f"({m.group(1)})", parent="(letter)",
            ordinal=(ord(m.group(1)) - ord("a") + 1,), end=m.end(),
        )  # fmt: skip
    return None


def follows(a: Enumerator, b: Enumerator) -> bool:
    """Whether `b` is the next label after `a` in the same sequence."""
    if a.parent != b.parent or a.kind != b.kind:
        return False
    if a.kind == "alnum":
        n, suffix = a.ordinal
        return b.ordinal in ((n, suffix + 1), (n + 1, 0))
    return b.ordinal == (a.ordinal[0] + 1,)


@dataclass(frozen=True)
class _Candidate:
    enumerator: Enumerator
    position: tuple[int, int]  # (element index, char offset)


class EnumeratorLattice:
    """Every enumerator in a document that its siblings corroborate.

    Built once over the pre-merge element stream. A candidate is corroborated
    when the member before it in document order is its predecessor or the
    member after it is its successor, within the same parent. That admits a
    sequence's first and last members, tolerates a restart (``(a)`` under a
    new clause), and rejects a lone cross-reference that happens to sit after
    a sentence terminator.
    """

    def __init__(self, corroborated: set[str]):
        self.corroborated = corroborated

    @classmethod
    def from_elements(cls, elements: list[Element]) -> EnumeratorLattice:
        groups: dict[str, list[_Candidate]] = defaultdict(list)
        for index, element in enumerate(elements):
            if element.type not in ("paragraph", "heading"):
                continue
            for offset, enumerator in _candidates(element.text):
                groups[enumerator.parent].append(_Candidate(enumerator, (index, offset)))

        corroborated: set[str] = set()
        for members in groups.values():
            members.sort(key=lambda c: c.position)
            for i, member in enumerate(members):
                before = members[i - 1].enumerator if i > 0 else None
                after = members[i + 1].enumerator if i + 1 < len(members) else None
                if (before is not None and follows(before, member.enumerator)) or (
                    after is not None and follows(member.enumerator, after)
                ):
                    corroborated.add(member.enumerator.label)
        return cls(corroborated)

    def opens(self, element: Element) -> Enumerator | None:
        """The corroborated enumerator this element's text starts with, if any."""
        enumerator = match_enumerator(element.text)
        if enumerator is not None and enumerator.label in self.corroborated:
            return enumerator
        return None

    def positions(self, text: str) -> list[int]:
        """Offsets inside `text` where a corroborated sectional enumerator opens
        a new clause: the points at which a welded element should be split."""
        return [
            offset
            for offset, enumerator in _candidates(text)
            if offset > 0 and enumerator.sectional and enumerator.label in self.corroborated
        ]


def _candidates(text: str) -> list[tuple[int, Enumerator]]:
    """Every enumerator-shaped string at the start of `text` or after a
    sentence terminator within it."""
    found: list[tuple[int, Enumerator]] = []
    first = match_enumerator(text)
    if first is not None:
        found.append((0, first))
    for m in _AFTER_TERMINATOR.finditer(text):
        enumerator = match_enumerator(text, m.end())
        if enumerator is not None:
            found.append((m.end(), enumerator))
    return found


__all__ = ["SECTIONAL", "Enumerator", "EnumeratorLattice", "follows", "match_enumerator"]
