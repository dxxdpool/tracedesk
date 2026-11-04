import uuid

from fastapi import APIRouter, status

from app.db import SessionDep
from app.deps import Actor
from app.models import Experiment, Hypothesis
from app.routes.common import get_or_404
from app.schemas import ExperimentCreate, ExperimentRead

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ExperimentRead)
async def request_experiment(body: ExperimentCreate, session: SessionDep, actor: Actor) -> Experiment:
    hypothesis = await get_or_404(session, Hypothesis, body.hypothesis_id, "hypothesis_not_found")
    # Status is left to the database default ('requested'); approval comes on Day 2.
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


@router.get("/{experiment_id}", response_model=ExperimentRead)
async def get_experiment(experiment_id: uuid.UUID, session: SessionDep) -> Experiment:
    return await get_or_404(session, Experiment, experiment_id, "experiment_not_found")
