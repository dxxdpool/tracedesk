import uuid

from fastapi import APIRouter, status

from app.db import SessionDep
from app.models import Investigation
from app.routes.common import get_or_404
from app.schemas import InvestigationCreate, InvestigationRead

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvestigationRead)
async def create_investigation(body: InvestigationCreate, session: SessionDep) -> Investigation:
    # Cloning and the pinned repo_sha come on Day 2.
    investigation = Investigation(**body.model_dump())
    session.add(investigation)
    await session.commit()
    await session.refresh(investigation)
    return investigation


@router.get("/{investigation_id}", response_model=InvestigationRead)
async def get_investigation(investigation_id: uuid.UUID, session: SessionDep) -> Investigation:
    return await get_or_404(session, Investigation, investigation_id, "investigation_not_found")
