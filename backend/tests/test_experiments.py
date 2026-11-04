import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import ACTOR


def experiment_request(hypothesis_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hypothesis_id": hypothesis_id,
        "command": "pytest tests/test_paginate.py -p no:cacheprovider",
        "why": "the failing test should show page 1 skipping item 1",
    }
    return payload | overrides


async def test_new_experiment_starts_requested_under_the_hypothesis_investigation(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    resp = await client.post("/experiments", json=experiment_request(hypothesis["id"]))

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "requested"
    assert body["investigation_id"] == hypothesis["investigation_id"]
    assert body["requested_by"] == ACTOR
    assert body["approved_by"] is None
    assert body["exit_code"] is None
    assert body["outcome"] is None


async def test_get_experiment_round_trip(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    created = await client.post("/experiments", json=experiment_request(hypothesis["id"]))

    resp = await client.get(f"/experiments/{created.json()['id']}")

    assert resp.status_code == 200
    assert resp.json() == created.json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"approved_by": "human"},
        {"outcome": "confirms"},
        {"exit_code": 0},
        {"command": "   "},
        {"why": ""},
    ],
    ids=["status", "approved-by", "outcome", "exit-code", "blank-command", "empty-why"],
)
async def test_create_experiment_rejects_server_owned_or_empty_fields(
    client: AsyncClient, hypothesis: dict[str, Any], overrides: dict[str, Any]
) -> None:
    resp = await client.post("/experiments", json=experiment_request(hypothesis["id"], **overrides))

    assert resp.status_code == 422


async def test_create_experiment_for_unknown_hypothesis_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/experiments", json=experiment_request(str(uuid.uuid4())))

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "hypothesis_not_found"


async def test_get_unknown_experiment_returns_404(client: AsyncClient) -> None:
    resp = await client.get(f"/experiments/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "experiment_not_found"
