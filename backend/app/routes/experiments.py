"""Experiments: requested by an agent, approved by the human, run on the host, then concluded.

requested --approve (human)--> approved --result--> completed | failed | timed_out --conclude--> outcome
          --reject  (human)--> rejected

The API never runs anything: `st experiment run` executes the approved command in the sandbox on
the host and uploads the result here. Each step locks the row, so two uploads or two approvals of the
same experiment run one after the other and the second one is refused.
"""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentRole
from app.config import settings
from app.db import SessionDep
from app.deps import Actor
from app.errors import ApiError
from app.models import (
    FINISHED_EXPERIMENT_STATUSES,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    Experiment,
    ExperimentOutcome,
    ExperimentStatus,
    Hypothesis,
    Investigation,
)
from app.routes.common import DEFAULT_PAGE_SIZE, Limit, Offset, get_or_404
from app.schemas import ExperimentConclusion, ExperimentCreate, ExperimentRead, ExperimentResult
from app.services.artifacts import log_excerpt, write_logs
from app.state_machine import VERIFY_ROLES, Role

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ExperimentRead)
async def request_experiment(body: ExperimentCreate, session: SessionDep, actor: Actor) -> Experiment:
    hypothesis = await get_or_404(session, Hypothesis, body.hypothesis_id, "hypothesis_not_found")
    # Status is left to the database default ('requested'); only the human can approve it.
    experiment = Experiment(
        investigation_id=hypothesis.investigation_id,
        hypothesis_id=hypothesis.id,
        command=body.command,
        why=body.why,
        requested_by=actor,
    )
    session.add(experiment)
    await session.commit()
    await session.refresh(experiment)
    return experiment


@router.get("", response_model=list[ExperimentRead])
async def list_experiments(
    investigation_id: uuid.UUID, session: SessionDep, limit: Limit = DEFAULT_PAGE_SIZE, offset: Offset = 0
) -> Sequence[Experiment]:
    await get_or_404(session, Investigation, investigation_id, "investigation_not_found")
    query = (
        select(Experiment)
        .where(Experiment.investigation_id == investigation_id)
        .order_by(Experiment.created_at, Experiment.id)
        .limit(limit)
        .offset(offset)
    )
    return (await session.scalars(query)).all()


@router.get("/{experiment_id}", response_model=ExperimentRead)
async def get_experiment(experiment_id: uuid.UUID, session: SessionDep) -> Experiment:
    return await get_or_404(session, Experiment, experiment_id, "experiment_not_found")


async def lock_experiment(session: AsyncSession, experiment_id: uuid.UUID) -> Experiment:
    experiment = await session.scalar(select(Experiment).where(Experiment.id == experiment_id).with_for_update())
    if experiment is None:
        raise ApiError(404, "experiment_not_found", f"No experiments row with id {experiment_id}")
    return experiment


async def decide(
    session: AsyncSession, experiment_id: uuid.UUID, role: Role, actor: str, decision: ExperimentStatus
) -> Experiment:
    """Approve or reject. Only the approver token (role human) may decide; agents never can."""
    if role != Role.HUMAN:
        raise ApiError(403, "role_not_allowed", "Only the human approver can approve or reject experiments")
    experiment = await lock_experiment(session, experiment_id)
    if experiment.status != ExperimentStatus.REQUESTED:
        raise ApiError(
            409,
            "experiment_not_requested",
            f"Only requested experiments can be decided; this one is '{experiment.status}'",
        )
    experiment.status = decision
    experiment.approved_by = actor
    await session.commit()
    await session.refresh(experiment)
    return experiment


@router.post("/{experiment_id}/approve", response_model=ExperimentRead)
async def approve_experiment(
    experiment_id: uuid.UUID, session: SessionDep, actor: Actor, role: CurrentRole
) -> Experiment:
    return await decide(session, experiment_id, role, actor, ExperimentStatus.APPROVED)


@router.post("/{experiment_id}/reject", response_model=ExperimentRead)
async def reject_experiment(
    experiment_id: uuid.UUID, session: SessionDep, actor: Actor, role: CurrentRole
) -> Experiment:
    return await decide(session, experiment_id, role, actor, ExperimentStatus.REJECTED)


@router.post("/{experiment_id}/result", response_model=ExperimentRead)
async def upload_result(
    experiment_id: uuid.UUID, body: ExperimentResult, session: SessionDep, actor: Actor
) -> Experiment:
    """Store the sandbox run: logs on disk, numbers on the row, and an experiment_output evidence row."""
    experiment = await lock_experiment(session, experiment_id)
    if experiment.status != ExperimentStatus.APPROVED:
        raise ApiError(
            409,
            "experiment_not_approved",
            f"Results are accepted only for approved experiments; this one is '{experiment.status}'",
        )
    investigation = await get_or_404(session, Investigation, experiment.investigation_id, "investigation_not_found")
    stdout_path, stderr_path = await asyncio.to_thread(
        write_logs, settings.artifacts_dir, experiment.investigation_id, experiment.id, body.stdout, body.stderr
    )
    experiment.status = body.status
    experiment.exit_code = body.exit_code
    experiment.duration_ms = body.duration_ms
    experiment.stdout_path = stdout_path
    experiment.stderr_path = stderr_path
    experiment.finished_at = datetime.now(UTC)
    exit_text = "no exit code" if body.exit_code is None else f"exit code {body.exit_code}"
    session.add(
        Evidence(
            investigation_id=experiment.investigation_id,
            hypothesis_id=experiment.hypothesis_id,
            experiment_id=experiment.id,
            kind=EvidenceKind.OBSERVATION,
            source_type=EvidenceSource.EXPERIMENT_OUTPUT,
            snippet=log_excerpt(body.stdout, body.stderr),
            note=f"sandbox run {body.status}, {exit_text}, {body.duration_ms} ms: {experiment.command}"[:1000],
            repo_sha=investigation.repo_sha,
            created_by=actor,
        )
    )
    await session.commit()
    await session.refresh(experiment)
    return experiment


@router.post("/{experiment_id}/conclude", response_model=ExperimentRead)
async def conclude_experiment(
    experiment_id: uuid.UUID, body: ExperimentConclusion, session: SessionDep, role: CurrentRole
) -> Experiment:
    """Say what the result means for the hypothesis. Set once; only a completed run can confirm or refute.

    Validator or human only: the agent that proposed a hypothesis must not judge its own experiment.
    """
    if role not in VERIFY_ROLES:
        raise ApiError(403, "role_not_allowed", "Only the validator or a human can conclude an experiment")
    experiment = await lock_experiment(session, experiment_id)
    if experiment.status not in FINISHED_EXPERIMENT_STATUSES:
        raise ApiError(
            409, "experiment_has_no_result", f"Upload a result first; this experiment is '{experiment.status}'"
        )
    if experiment.outcome is not None:
        raise ApiError(
            409, "experiment_already_concluded", f"This experiment was already concluded as '{experiment.outcome}'"
        )
    if body.outcome != ExperimentOutcome.INCONCLUSIVE and experiment.status != ExperimentStatus.COMPLETED:
        raise ApiError(
            409, "experiment_not_completed", f"A '{experiment.status}' run can only be concluded as inconclusive"
        )
    experiment.outcome = body.outcome
    await session.commit()
    await session.refresh(experiment)
    return experiment
