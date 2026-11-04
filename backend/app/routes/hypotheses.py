import uuid
from collections.abc import Sequence

from fastapi import APIRouter, status
from sqlalchemy import select

from app.db import SessionDep
from app.deps import Actor
from app.models import Hypothesis, Investigation
from app.routes.common import DEFAULT_PAGE_SIZE, Limit, Offset, get_or_404
from app.schemas import HypothesisCreate, HypothesisRead

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
