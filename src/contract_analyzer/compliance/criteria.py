"""The compliance criteria, read once from `criteria.json`.

A criterion carries the assignment's question verbatim -- the result's
`compliance_question` must equal it, and the validator checks that -- and
the sub-requirements its prose enumerates, each with a stable id the model
echoes back. Ids are what make the derived state checkable: a draft that
renames or drops a sub-requirement is a structural error, not a judgement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CRITERIA_PATH = Path(__file__).with_name("criteria.json")


@dataclass(frozen=True)
class SubRequirement:
    id: str
    requirement: str


@dataclass(frozen=True)
class Criterion:
    id: str
    requirement: str
    question: str
    sub_requirements: tuple[SubRequirement, ...]
    states: tuple[str, ...]

    def sub_requirement_ids(self) -> list[str]:
        return [s.id for s in self.sub_requirements]

    def sub_requirements_text(self) -> str:
        """The list as the prompt shows it: `- id: requirement`."""
        return "\n".join(f"- {s.id}: {s.requirement}" for s in self.sub_requirements)


@lru_cache(maxsize=1)
def get_criteria(path: Path = CRITERIA_PATH) -> tuple[Criterion, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    criteria = tuple(
        Criterion(
            id=item["id"],
            requirement=item["compliance_requirement"],
            question=item["description"],
            sub_requirements=tuple(
                SubRequirement(id=s["id"], requirement=s["requirement"])
                for s in item["sub_requirements"]
            ),
            states=tuple(item["compliance_state_options"]),
        )
        for item in raw
    )
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate criterion ids in {path}")
    for criterion in criteria:
        sub_ids = criterion.sub_requirement_ids()
        if not sub_ids or len(set(sub_ids)) != len(sub_ids):
            raise ValueError(f"criterion {criterion.id}: sub-requirement ids must be unique")
    return criteria


def get_criterion(criterion_id: str) -> Criterion:
    for criterion in get_criteria():
        if criterion.id == criterion_id:
            return criterion
    raise KeyError(
        f"no criterion {criterion_id!r}; known: {', '.join(c.id for c in get_criteria())}"
    )


__all__ = ["CRITERIA_PATH", "Criterion", "SubRequirement", "get_criteria", "get_criterion"]
