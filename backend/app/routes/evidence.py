import asyncio
import uuid
from collections.abc import Sequence

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionDep
from app.deps import Actor
from app.errors import ApiError
from app.models import Evidence, EvidenceSource, Experiment, Hypothesis, Investigation
from app.routes.common import DEFAULT_PAGE_SIZE, Limit, Offset, get_or_404, workspace_api_error, workspace_of
from app.schemas import EvidenceCreate, EvidenceRead
from app.services.citations import Citation, capture_code_ref, capture_commit
from app.services.workspace import WorkspaceError

router = APIRouter(prefix="/evidence", tags=["evidence"])


async def resolve_hypothesis_id(session: AsyncSession, body: EvidenceCreate) -> uuid.UUID | None:
    """Check every linked row exists and belongs together; return the hypothesis to link.

    Evidence citing an experiment is linked to that experiment's hypothesis. The guards in
    Part C count evidence and experiments per hypothesis, so a mismatched pair would corrupt them.
    """
    await get_or_404(session, Investigation, body.investigation_id, "investigation_not_found")
    if body.hypothesis_id is not None:
        hypothesis = await get_or_404(session, Hypothesis, body.hypothesis_id, "hypothesis_not_found")
        if hypothesis.investigation_id != body.investigation_id:
            raise ApiError(422, "hypothesis_not_in_investigation", "The hypothesis belongs to another investigation")
    if body.experiment_id is None:
        return body.hypothesis_id

    experiment = await get_or_404(session, Experiment, body.experiment_id, "experiment_not_found")
    if experiment.investigation_id != body.investigation_id:
        raise ApiError(422, "experiment_not_in_investigation", "The experiment belongs to another investigation")
    if body.hypothesis_id is not None and experiment.hypothesis_id != body.hypothesis_id:
        raise ApiError(422, "experiment_not_for_hypothesis", "The experiment tests a different hypothesis")
    return experiment.hypothesis_id


async def capture_citation(investigation: Investigation, body: EvidenceCreate) -> Citation | None:
    """Check a code_ref / git_history citation against the pinned commit; the server quotes it itself."""
    # EvidenceCreate already enforced which fields each source_type carries.
    file_path, start, end, commit = body.file_path, body.line_start, body.line_end, body.commit_sha
    try:
        if body.source_type == EvidenceSource.CODE_REF and file_path and start and end:
            ws = workspace_of(investigation)
            return await asyncio.to_thread(capture_code_ref, ws, file_path, start, end)
        if body.source_type == EvidenceSource.GIT_HISTORY and commit:
            ws = workspace_of(investigation)
            return await asyncio.to_thread(capture_commit, ws, commit, file_path)
    except WorkspaceError as exc:
        raise workspace_api_error(exc, not_found_status=422) from exc
    return None


@router.post("", status_code=status.HTTP_201_CREATED, response_model=EvidenceRead)
async def create_evidence(body: EvidenceCreate, session: SessionDep, actor: Actor) -> Evidence:
    hypothesis_id = await resolve_hypothesis_id(session, body)
    investigation = await get_or_404(session, Investigation, body.investigation_id, "investigation_not_found")
    citation = await capture_citation(investigation, body)
    fields = body.model_dump(exclude={"hypothesis_id", "file_path", "commit_sha"})
    evidence = Evidence(**fields, hypothesis_id=hypothesis_id, created_by=actor)
    if citation is not None:
        evidence.file_path = citation.file_path
        evidence.commit_sha = citation.commit_sha
        evidence.snippet = citation.snippet
        evidence.repo_sha = investigation.repo_sha
    session.add(evidence)
    await session.commit()
    await session.refresh(evidence)
    return evidence


@router.get("", response_model=list[EvidenceRead])
async def list_evidence(
    investigation_id: uuid.UUID,
    session: SessionDep,
    hypothesis_id: uuid.UUID | None = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
    offset: Offset = 0,
) -> Sequence[Evidence]:
    await get_or_404(session, Investigation, investigation_id, "investigation_not_found")
    query = select(Evidence).where(Evidence.investigation_id == investigation_id)
    if hypothesis_id is not None:
        query = query.where(Evidence.hypothesis_id == hypothesis_id)
    query = query.order_by(Evidence.created_at, Evidence.id).limit(limit).offset(offset)
    return (await session.scalars(query)).all()
