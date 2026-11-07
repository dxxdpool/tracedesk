import uuid

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.db import SessionDep
from app.models import Evidence, Experiment, Hypothesis, HypothesisEvent, Investigation
from app.routes.common import get_or_404
from app.services.report import ReportData, render_report

router = APIRouter(prefix="/investigations", tags=["reports"])


@router.get("/{investigation_id}/report", response_class=PlainTextResponse)
async def investigation_report(investigation_id: uuid.UUID, session: SessionDep) -> PlainTextResponse:
    """Markdown report built from the ledger: five queries, one per table, no per-row loading."""
    investigation = await get_or_404(session, Investigation, investigation_id, "investigation_not_found")
    hypotheses = (
        await session.scalars(
            select(Hypothesis)
            .where(Hypothesis.investigation_id == investigation_id)
            .order_by(Hypothesis.created_at, Hypothesis.id)
        )
    ).all()
    events = (
        await session.scalars(
            select(HypothesisEvent)
            .join(Hypothesis, Hypothesis.id == HypothesisEvent.hypothesis_id)
            .where(Hypothesis.investigation_id == investigation_id)
            .order_by(HypothesisEvent.created_at, HypothesisEvent.id)
        )
    ).all()
    evidence = (
        await session.scalars(
            select(Evidence)
            .where(Evidence.investigation_id == investigation_id)
            .order_by(Evidence.created_at, Evidence.id)
        )
    ).all()
    experiments = (
        await session.scalars(
            select(Experiment)
            .where(Experiment.investigation_id == investigation_id)
            .order_by(Experiment.created_at, Experiment.id)
        )
    ).all()
    data = ReportData(investigation, hypotheses, events, evidence, experiments)
    return PlainTextResponse(render_report(data), media_type="text/markdown; charset=utf-8")
