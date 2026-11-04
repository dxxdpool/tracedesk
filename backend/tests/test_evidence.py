import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import ACTOR


def code_ref(investigation_id: str, hypothesis_id: str | None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "investigation_id": investigation_id,
        "hypothesis_id": hypothesis_id,
        "kind": "supporting",
        "source_type": "code_ref",
        "file_path": "pkg/paginate.py",
        "line_start": 10,
        "line_end": 14,
        "note": "start = (page - 1) * size + 1",
    }
    return payload | overrides


async def test_create_evidence_linked_to_hypothesis(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    resp = await client.post("/evidence", json=code_ref(hypothesis["investigation_id"], hypothesis["id"]))

    assert resp.status_code == 201
    body = resp.json()
    assert body["hypothesis_id"] == hypothesis["id"]
    assert (body["line_start"], body["line_end"]) == (10, 14)
    assert body["created_by"] == ACTOR
    assert body["snippet"] is None  # captured server-side from the pinned commit on Day 2


async def test_create_observation_without_hypothesis_or_lines(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    payload = {
        "investigation_id": investigation["id"],
        "kind": "observation",
        "source_type": "human_note",
        "note": "only happens on page 1",
    }

    resp = await client.post("/evidence", json=payload)

    assert resp.status_code == 201
    assert resp.json()["hypothesis_id"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"line_start": 14, "line_end": 10},
        {"line_start": 10, "line_end": None},
        {"line_start": None, "line_end": 10},
        {"line_start": 0, "line_end": 3},
        {"kind": "certain"},
        {"source_type": "vibes"},
        {"snippet": "return items[1:]"},
        {"repo_sha": "a" * 40},
        {"note": "  "},
    ],
    ids=[
        "reversed-range", "start-only", "end-only", "zero-line", "bad-kind", "bad-source",
        "client-snippet", "client-sha", "blank-note",
    ],
)
async def test_create_evidence_rejects_invalid_payload(
    client: AsyncClient, hypothesis: dict[str, Any], overrides: dict[str, Any]
) -> None:
    payload = code_ref(hypothesis["investigation_id"], hypothesis["id"], **overrides)

    resp = await client.post("/evidence", json=payload)

    assert resp.status_code == 422


async def test_create_evidence_for_unknown_investigation_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/evidence", json=code_ref(str(uuid.uuid4()), None))

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "investigation_not_found"


async def test_create_evidence_for_unknown_hypothesis_returns_404(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post("/evidence", json=code_ref(investigation["id"], str(uuid.uuid4())))

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "hypothesis_not_found"


async def test_create_evidence_rejects_hypothesis_from_another_investigation(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    other = await client.post(
        "/investigations", json={"repo_url": "https://github.com/example/other", "problem": "other bug"}
    )

    resp = await client.post("/evidence", json=code_ref(other.json()["id"], hypothesis["id"]))

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "hypothesis_not_in_investigation"


async def test_create_evidence_for_unknown_experiment_returns_404(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    payload = code_ref(hypothesis["investigation_id"], hypothesis["id"], experiment_id=str(uuid.uuid4()))

    resp = await client.post("/evidence", json=payload)

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "experiment_not_found"


async def make_experiment(client: AsyncClient, hypothesis_id: str) -> dict[str, Any]:
    resp = await client.post(
        "/experiments", json={"hypothesis_id": hypothesis_id, "command": "pytest", "why": "repro"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_evidence_rejects_experiment_of_another_hypothesis(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    inv_id = hypothesis["investigation_id"]
    other = await client.post("/hypotheses", json={"investigation_id": inv_id, "text": "decoy"})
    other_experiment = await make_experiment(client, other.json()["id"])

    resp = await client.post(
        "/evidence", json=code_ref(inv_id, hypothesis["id"], experiment_id=other_experiment["id"])
    )

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "experiment_not_for_hypothesis"


async def test_evidence_citing_only_an_experiment_is_linked_to_its_hypothesis(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    experiment = await make_experiment(client, hypothesis["id"])

    resp = await client.post(
        "/evidence",
        json={
            "investigation_id": hypothesis["investigation_id"],
            "experiment_id": experiment["id"],
            "kind": "observation",
            "source_type": "experiment_output",
            "note": "1 failed",
        },
    )

    assert resp.status_code == 201
    assert resp.json()["hypothesis_id"] == hypothesis["id"]


@pytest.mark.parametrize(
    "overrides",
    [{"note": "bad\u0000byte"}, {"file_path": "pkg/\u0000.py"}, {"line_start": 1, "line_end": 2**31}],
    ids=["nul-in-note", "nul-in-path", "line-overflows-int"],
)
async def test_create_evidence_rejects_values_postgres_cannot_store(
    client: AsyncClient, hypothesis: dict[str, Any], overrides: dict[str, Any]
) -> None:
    payload = code_ref(hypothesis["investigation_id"], hypothesis["id"], **overrides)

    resp = await client.post("/evidence", json=payload)

    assert resp.status_code == 422


async def test_list_evidence_filters_by_hypothesis(
    client: AsyncClient, hypothesis: dict[str, Any]
) -> None:
    inv_id = hypothesis["investigation_id"]
    linked = await client.post("/evidence", json=code_ref(inv_id, hypothesis["id"]))
    await client.post("/evidence", json=code_ref(inv_id, None, kind="observation"))

    all_rows = await client.get("/evidence", params={"investigation_id": inv_id})
    linked_rows = await client.get(
        "/evidence", params={"investigation_id": inv_id, "hypothesis_id": hypothesis["id"]}
    )

    assert all_rows.status_code == 200
    assert len(all_rows.json()) == 2
    assert [e["id"] for e in linked_rows.json()] == [linked.json()["id"]]
