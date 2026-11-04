import uuid
from typing import Any

from httpx import AsyncClient

from tests.conftest import ACTOR


async def test_new_hypothesis_starts_proposed_and_records_actor(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post(
        "/hypotheses", json={"investigation_id": investigation["id"], "text": "off-by-one in start"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["created_by"] == ACTOR
    assert body["investigation_id"] == investigation["id"]


async def test_hypothesis_status_cannot_be_set_on_create(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post(
        "/hypotheses",
        json={"investigation_id": investigation["id"], "text": "it is the cache", "status": "verified"},
    )

    assert resp.status_code == 422


async def test_create_hypothesis_without_actor_header_returns_422(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post(
        "/hypotheses",
        json={"investigation_id": investigation["id"], "text": "it is the cache"},
        headers={"X-Actor": ""},
    )

    assert resp.status_code == 422


async def test_create_hypothesis_with_blank_actor_returns_422(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post(
        "/hypotheses",
        json={"investigation_id": investigation["id"], "text": "it is the cache"},
        headers={"X-Actor": "   "},
    )

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "actor_missing"


async def test_create_hypothesis_rejects_nul_character(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post(
        "/hypotheses", json={"investigation_id": investigation["id"], "text": "bad\u0000byte"}
    )

    assert resp.status_code == 422


async def test_list_hypotheses_pages_with_offset(client: AsyncClient, investigation: dict[str, Any]) -> None:
    for text in ("first", "second", "third"):
        await client.post("/hypotheses", json={"investigation_id": investigation["id"], "text": text})

    page = await client.get(
        "/hypotheses", params={"investigation_id": investigation["id"], "limit": 2, "offset": 2}
    )

    assert page.status_code == 200
    assert [h["text"] for h in page.json()] == ["third"]


async def test_create_hypothesis_for_unknown_investigation_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/hypotheses", json={"investigation_id": str(uuid.uuid4()), "text": "x"})

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "investigation_not_found"


async def test_get_hypothesis_round_trip(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    resp = await client.get(f"/hypotheses/{hypothesis['id']}")

    assert resp.status_code == 200
    assert resp.json() == hypothesis


async def test_get_unknown_hypothesis_returns_404(client: AsyncClient) -> None:
    resp = await client.get(f"/hypotheses/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "hypothesis_not_found"


async def test_list_hypotheses_returns_only_that_investigations_rows(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    other = await client.post(
        "/investigations", json={"repo_url": "https://github.com/example/other", "problem": "other bug"}
    )
    await client.post("/hypotheses", json={"investigation_id": other.json()["id"], "text": "unrelated"})

    resp = await client.get("/hypotheses", params={"investigation_id": hypothesis["investigation_id"]})

    assert resp.status_code == 200
    assert [h["id"] for h in resp.json()] == [hypothesis["id"]]


async def test_list_hypotheses_requires_investigation_id(client: AsyncClient) -> None:
    resp = await client.get("/hypotheses")

    assert resp.status_code == 422


async def test_list_hypotheses_for_unknown_investigation_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/hypotheses", params={"investigation_id": str(uuid.uuid4())})

    assert resp.status_code == 404
