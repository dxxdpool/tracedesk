import uuid
from collections.abc import Sequence

from fastapi import APIRouter, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentRole
from app.db import SessionDep
from app.deps import Actor
from app.errors import ApiError
from app.models import (
    Evidence,
    EvidenceKind,
    Experiment,
    ExperimentStatus,
    Hypothesis,
    HypothesisEvent,
    Investigation,
)
from app.routes.common import DEFAULT_PAGE_SIZE, Limit, Offset, get_or_404
from app.schemas import HypothesisCreate, HypothesisEventRead, HypothesisRead, TransitionRequest
from app.state_machine import ExperimentFacts, Role, TransitionContext, can_transition

router = APIRouter(prefix="/hypotheses", tags=["hypotheses"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=HypothesisRead)
async def create_hypothesis(body: HypothesisCreate, session: SessionDep, actor: Actor) -> Hypothesis:
    await get_or_404(session, Investigation, body.investigation_id, "investigation_not_found")
    # Status is left to the database default ('proposed'); it changes only through /transition.
    hypothesis = Hypothesis(investigation_id=body.investigation_id, text=body.text, created_by=actor)
    session.add(hypothesis)
    await session.commit()
    await session.refresh(hypothesis)
    return hypothesis


@router.get("", response_model=list[HypothesisRead])
async def list_hypotheses(
    investigation_id: uuid.UUID, session: SessionDep, limit: Limit = DEFAULT_PAGE_SIZE, offset: Offset = 0
) -> Sequence[Hypothesis]:
    await get_or_404(session, Investigation, investigation_id, "investigation_not_found")
    query = (
        select(Hypothesis)
        .where(Hypothesis.investigation_id == investigation_id)
        .order_by(Hypothesis.created_at, Hypothesis.id)
        .limit(limit)
        .offset(offset)
    )
    return (await session.scalars(query)).all()


@router.get("/{hypothesis_id}", response_model=HypothesisRead)
async def get_hypothesis(hypothesis_id: uuid.UUID, session: SessionDep) -> Hypothesis:
    return await get_or_404(session, Hypothesis, hypothesis_id, "hypothesis_not_found")


async def load_transition_context(session: AsyncSession, hypothesis_id: uuid.UUID, role: Role) -> TransitionContext:
    """Gather the facts the state machine needs. Only completed experiments can confirm anything."""
    counts_query = (
        select(Evidence.kind, func.count())
        .where(Evidence.hypothesis_id == hypothesis_id)
        .group_by(Evidence.kind)
    )
    counts: dict[EvidenceKind, int] = {kind: count for kind, count in await session.execute(counts_query)}
    experiments_query = select(Experiment.status, Experiment.exit_code, Experiment.outcome).where(
        Experiment.hypothesis_id == hypothesis_id, Experiment.status == ExperimentStatus.COMPLETED
    )
    experiments = tuple(
        ExperimentFacts(status=row_status, exit_code=exit_code, outcome=outcome)
        for row_status, exit_code, outcome in await session.execute(experiments_query)
    )
    return TransitionContext(
        supporting_count=counts.get(EvidenceKind.SUPPORTING, 0),
        contradicting_count=counts.get(EvidenceKind.CONTRADICTING, 0),
        experiments=experiments,
        actor_role=role,
    )


@router.post("/{hypothesis_id}/transition", response_model=HypothesisRead)
async def transition_hypothesis(
    hypothesis_id: uuid.UUID, body: TransitionRequest, session: SessionDep, actor: Actor, role: CurrentRole
) -> Hypothesis:
    # FOR UPDATE: two concurrent transitions on one hypothesis run one after the other, so the
    # second sees the first one's new status instead of passing the guards against a stale one.
    locked = select(Hypothesis).where(Hypothesis.id == hypothesis_id).with_for_update()
    hypothesis = await session.scalar(locked)
    if hypothesis is None:
        raise ApiError(404, "hypothesis_not_found", f"No hypotheses row with id {hypothesis_id}")

    context = await load_transition_context(session, hypothesis_id, role)
    result = can_transition(hypothesis.status, body.to, context)
    if not result.ok:
        raise ApiError(409, result.reason_code or "illegal_transition", result.message or "Transition refused")

    # Status change and log entry commit together, or not at all.
    session.add(
        HypothesisEvent(
            hypothesis_id=hypothesis.id,
            from_status=hypothesis.status,
            to_status=body.to,
            actor=actor,
            reason=body.reason,
        )
    )
    hypothesis.status = body.to
    await session.commit()
    await session.refresh(hypothesis)
    return hypothesis


@router.get("/{hypothesis_id}/events", response_model=list[HypothesisEventRead])
async def list_hypothesis_events(
    hypothesis_id: uuid.UUID, session: SessionDep, limit: Limit = DEFAULT_PAGE_SIZE, offset: Offset = 0
) -> Sequence[HypothesisEvent]:
    await get_or_404(session, Hypothesis, hypothesis_id, "hypothesis_not_found")
    query = (
        select(HypothesisEvent)
        .where(HypothesisEvent.hypothesis_id == hypothesis_id)
        .order_by(HypothesisEvent.created_at, HypothesisEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return (await session.scalars(query)).all()
