"""POST /hypotheses/{id}/transition and GET /hypotheses/{id}/events against the real database."""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Experiment, ExperimentOutcome, ExperimentStatus, HypothesisEvent
from app.state_machine import Role
from tests.conftest import ACTOR, bearer

MISSING_ID = "00000000-0000-0000-0000-000000000000"


async def move(client: AsyncClient, hypothesis_id: str, to: str, role: Role = Role.AGENT) -> Any:
    resp = await client.post(
        f"/hypotheses/{hypothesis_id}/transition", json={"to": to, "reason": f"move to {to}"}, headers=bearer(role)
    )
    return resp


async def add_evidence(client: AsyncClient, hypothesis: dict[str, Any], kind: str) -> None:
    resp = await client.post(
        "/evidence",
        json={
            "investigation_id": hypothesis["investigation_id"],
            "hypothesis_id": hypothesis["id"],
            "kind": kind,
            "source_type": "code_ref",
            "file_path": "pkg/paginate.py",
            "line_start": 10,
            "line_end": 12,
        },
    )
    assert resp.status_code == 201, resp.text


async def add_experiment(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    hypothesis_id: str,
    *,
    status: ExperimentStatus,
    exit_code: int | None,
    outcome: ExperimentOutcome | None,
    approved_by: str | None = "human",
) -> None:
    """Request through the API, then write the result fields directly, so the guard is tested on
    exact rows (the full approve/run/conclude flow is tested in test_experiments.py)."""
    resp = await client.post(
        "/experiments", json={"hypothesis_id": hypothesis_id, "command": "pytest -q", "why": "reproduce"}
    )
    assert resp.status_code == 201, resp.text
    async with session_factory() as session:
        await session.execute(
            update(Experiment)
            .where(Experiment.id == uuid.UUID(resp.json()["id"]))
            .values(status=status, exit_code=exit_code, outcome=outcome, approved_by=approved_by)
        )
        await session.commit()


async def to_supported(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    await add_evidence(client, hypothesis, "supporting")
    assert (await move(client, hypothesis["id"], "under_review")).status_code == 200
    assert (await move(client, hypothesis["id"], "supported")).status_code == 200


async def test_transition_updates_status_and_logs_event(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    resp = await client.post(
        f"/hypotheses/{hypothesis['id']}/transition", json={"to": "under_review", "reason": "worth a look"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "under_review"

    events = (await client.get(f"/hypotheses/{hypothesis['id']}/events")).json()
    assert len(events) == 1
    assert events[0]["from_status"] == "proposed"
    assert events[0]["to_status"] == "under_review"
    assert events[0]["actor"] == ACTOR
    assert events[0]["reason"] == "worth a look"


async def test_illegal_transition_returns_409_and_changes_nothing(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    resp = await move(client, hypothesis["id"], "supported")
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason_code"] == "illegal_transition"
    assert "proposed" in body["message"]

    assert (await client.get(f"/hypotheses/{hypothesis['id']}")).json()["status"] == "proposed"
    assert (await client.get(f"/hypotheses/{hypothesis['id']}/events")).json() == []


async def test_supported_without_evidence_returns_409(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    await move(client, hypothesis["id"], "under_review")
    resp = await move(client, hypothesis["id"], "supported")
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "no_supporting_evidence"


async def test_observation_evidence_does_not_count_as_supporting(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    await add_evidence(client, hypothesis, "observation")
    await move(client, hypothesis["id"], "under_review")
    assert (await move(client, hypothesis["id"], "supported")).json()["reason_code"] == "no_supporting_evidence"


async def test_contradicted_needs_contradicting_evidence(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    await move(client, hypothesis["id"], "under_review")
    assert (await move(client, hypothesis["id"], "contradicted")).json()["reason_code"] == "no_contradicting_evidence"
    await add_evidence(client, hypothesis, "contradicting")
    assert (await move(client, hypothesis["id"], "contradicted")).status_code == 200


async def test_evidence_on_other_hypothesis_does_not_count(
    client: AsyncClient, investigation: dict[str, Any], hypothesis: dict[str, Any]
) -> None:
    other = (
        await client.post("/hypotheses", json={"investigation_id": investigation["id"], "text": "cache is stale"})
    ).json()
    await add_evidence(client, other, "supporting")
    await move(client, hypothesis["id"], "under_review")
    assert (await move(client, hypothesis["id"], "supported")).json()["reason_code"] == "no_supporting_evidence"


async def test_verified_without_experiment_returns_409(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    await to_supported(client, hypothesis)
    resp = await move(client, hypothesis["id"], "verified", Role.VALIDATOR)
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "no_confirming_experiment"


async def test_agent_token_cannot_verify_even_with_confirming_experiment(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], hypothesis: dict[str, Any]
) -> None:
    await to_supported(client, hypothesis)
    await add_experiment(
        client, session_factory, hypothesis["id"],
        status=ExperimentStatus.COMPLETED, exit_code=1, outcome=ExperimentOutcome.CONFIRMS,
    )  # fmt: skip
    # Calling yourself "validator" in X-Actor does not help: the role comes from the token.
    resp = await client.post(
        f"/hypotheses/{hypothesis['id']}/transition",
        json={"to": "verified", "reason": "I am sure"},
        headers={"X-Actor": "validator", **bearer(Role.AGENT)},
    )
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "role_not_allowed"


@pytest.mark.parametrize(
    ("status", "exit_code", "outcome"),
    [
        (ExperimentStatus.COMPLETED, 0, ExperimentOutcome.INCONCLUSIVE),
        (ExperimentStatus.COMPLETED, 0, ExperimentOutcome.REFUTES),
        (ExperimentStatus.FAILED, 1, ExperimentOutcome.CONFIRMS),
        (ExperimentStatus.REQUESTED, None, None),
    ],
)
async def test_verified_rejected_with_non_confirming_experiment(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    hypothesis: dict[str, Any],
    status: ExperimentStatus,
    exit_code: int | None,
    outcome: ExperimentOutcome | None,
) -> None:
    await to_supported(client, hypothesis)
    await add_experiment(client, session_factory, hypothesis["id"], status=status, exit_code=exit_code, outcome=outcome)
    resp = await move(client, hypothesis["id"], "verified", Role.HUMAN)
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "no_confirming_experiment"


async def test_verified_rejected_when_experiment_was_never_approved(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], hypothesis: dict[str, Any]
) -> None:
    await to_supported(client, hypothesis)
    await add_experiment(
        client, session_factory, hypothesis["id"],
        status=ExperimentStatus.COMPLETED, exit_code=1, outcome=ExperimentOutcome.CONFIRMS, approved_by=None,
    )  # fmt: skip
    resp = await move(client, hypothesis["id"], "verified", Role.VALIDATOR)
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "no_confirming_experiment"


@pytest.mark.parametrize("role", [Role.VALIDATOR, Role.HUMAN])
async def test_verified_with_confirming_experiment(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], hypothesis: dict[str, Any], role: Role
) -> None:
    await to_supported(client, hypothesis)
    await add_experiment(
        client, session_factory, hypothesis["id"],
        status=ExperimentStatus.COMPLETED, exit_code=1, outcome=ExperimentOutcome.CONFIRMS,
    )  # fmt: skip
    resp = await move(client, hypothesis["id"], "verified", role)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "verified"

    events = (await client.get(f"/hypotheses/{hypothesis['id']}/events")).json()
    assert [e["to_status"] for e in events] == ["under_review", "supported", "verified"]

    # Terminal: nothing moves it again, not even a human.
    again = await move(client, hypothesis["id"], "under_review", Role.HUMAN)
    assert again.status_code == 409
    assert again.json()["reason_code"] == "hypothesis_terminal"


async def test_experiment_on_other_hypothesis_does_not_verify(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    investigation: dict[str, Any],
    hypothesis: dict[str, Any],
) -> None:
    other = (
        await client.post("/hypotheses", json={"investigation_id": investigation["id"], "text": "cache is stale"})
    ).json()
    await add_experiment(
        client, session_factory, other["id"],
        status=ExperimentStatus.COMPLETED, exit_code=1, outcome=ExperimentOutcome.CONFIRMS,
    )  # fmt: skip
    await to_supported(client, hypothesis)
    assert (await move(client, hypothesis["id"], "verified", Role.HUMAN)).json()["reason_code"] == (
        "no_confirming_experiment"
    )


async def test_rejected_transition_writes_no_event(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], hypothesis: dict[str, Any]
) -> None:
    await move(client, hypothesis["id"], "verified", Role.HUMAN)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(HypothesisEvent)) == 0


async def test_transition_unknown_hypothesis_404(client: AsyncClient) -> None:
    resp = await move(client, MISSING_ID, "under_review")
    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "hypothesis_not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "under_review"},
        {"to": "under_review", "reason": "   "},
        {"to": "maybe", "reason": "x"},
        {"to": "under_review", "reason": "x", "confidence": 0.99},
    ],
    ids=["no-reason", "blank-reason", "unknown-status", "extra-field"],
)
async def test_transition_invalid_payload_422(
    client: AsyncClient, hypothesis: dict[str, Any], payload: dict[str, Any]
) -> None:
    resp = await client.post(f"/hypotheses/{hypothesis['id']}/transition", json=payload)
    assert resp.status_code == 422


async def test_transition_needs_actor(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    resp = await client.post(
        f"/hypotheses/{hypothesis['id']}/transition",
        json={"to": "under_review", "reason": "x"},
        headers={"X-Actor": " "},
    )
    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "actor_missing"


async def test_events_unknown_hypothesis_404(client: AsyncClient) -> None:
    resp = await client.get(f"/hypotheses/{MISSING_ID}/events")
    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "hypothesis_not_found"
