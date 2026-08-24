"""What is running, and what the five questions are.

Both endpoints are open even when `API_KEY` is set: a healthcheck that needs a
credential is a healthcheck that fails for the wrong reason, and the criteria
are the vocabulary a client needs before it can authenticate meaningfully.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...compliance.criteria import get_criteria
from ...documents import list_documents
from ..deps import ConnDep, RunnerDep, SettingsDep
from ..schemas import CriterionOut, Health, SubRequirementOut

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness, configuration and counts")
def health(settings: SettingsDep, conn: ConnDep, runner: RunnerDep) -> Health:
    """Whether the service can work, and with what.

    `key_present` is the question a UI actually asks -- upload and retrieval
    work without an answer key, analysis and chat do not -- so it is reported
    rather than left to be discovered by a 503 three clicks later.
    """
    try:
        documents = len(list_documents(conn))
        db_ok = True
    except Exception:  # noqa: BLE001 - the point of the endpoint is to report this
        documents, db_ok = 0, False
    return Health(
        status="ok" if db_ok else "degraded",
        version=_version(),
        db=db_ok,
        embedder=settings.embedding_provider,
        embedding_model=settings.resolved_embedding_model,
        answer_model=settings.answer_model,
        key_present=bool(settings.anthropic_key),
        auth_required=bool(settings.api_key_value),
        documents=documents,
        analyses_running=runner.active,
    )


@router.get("/criteria", summary="The five compliance criteria")
def criteria() -> list[CriterionOut]:
    """Each criterion with its sub-requirements.

    The sub-requirements are the part worth publishing: the overall state is
    *derived* from them rather than asserted, so a client that shows them shows
    why a contract was judged partially compliant instead of only that it was.
    """
    return [
        CriterionOut(
            id=c.id,
            requirement=c.requirement,
            question=c.question,
            sub_requirements=[
                SubRequirementOut(id=s.id, requirement=s.requirement)
                for s in c.sub_requirements
            ],
            states=list(c.states),
        )
        for c in get_criteria()
    ]


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("contract-analyzer")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree
        return "0.0.0+source"
