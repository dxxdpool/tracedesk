import uuid
from typing import Any

import pytest
from httpx import AsyncClient


async def test_create_investigation_returns_open_investigation_with_uuid(client: AsyncClient) -> None:
    resp = await client.post(
        "/investigations",
        json={"repo_url": "https://github.com/example/repo", "problem": "totals are off by a cent"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["id"])
    assert body["status"] == "open"
    assert body["repo_sha"] is None
    assert body["problem"] == "totals are off by a cent"


async def test_get_investigation_round_trip(client: AsyncClient, investigation: dict[str, Any]) -> None:
    resp = await client.get(f"/investigations/{investigation['id']}")

    assert resp.status_code == 200
    assert resp.json() == investigation


async def test_get_unknown_investigation_returns_404_with_reason(client: AsyncClient) -> None:
    resp = await client.get(f"/investigations/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "investigation_not_found"


async def test_get_investigation_with_malformed_id_returns_422(client: AsyncClient) -> None:
    resp = await client.get("/investigations/not-a-uuid")

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"repo_url": "https://github.com/example/repo", "problem": ""},
        {"repo_url": "https://github.com/example/repo", "problem": "   "},
        {"repo_url": "", "problem": "broken"},
        {"problem": "broken"},
        {"repo_url": "https://github.com/example/repo", "problem": "broken", "status": "concluded"},
        {"repo_url": "https://github.com/example/repo", "problem": "broken", "repo_sha": "a" * 40},
        {"repo_url": "https://github.com/example/repo", "problem": "bad\u0000byte"},
        {"repo_url": "https://github.com/example/repo", "problem": "broken", "switch_room_id": ""},
    ],
    ids=[
        "empty-problem", "blank-problem", "empty-url", "missing-url", "status-field", "sha-field",
        "nul-in-problem", "empty-room-id",
    ],
)
async def test_create_investigation_rejects_invalid_payload(
    client: AsyncClient, payload: dict[str, Any]
) -> None:
    resp = await client.post("/investigations", json=payload)

    assert resp.status_code == 422
