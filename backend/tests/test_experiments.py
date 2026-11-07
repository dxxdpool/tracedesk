import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import settings
from app.schemas import MAX_LOG_CHARS
from app.state_machine import Role
from tests.conftest import ACTOR, bearer

HUMAN = {"X-Actor": "human", **bearer(Role.HUMAN)}
VALIDATOR = {"X-Actor": "validator", **bearer(Role.VALIDATOR)}
FAILING_RUN = {
    "status": "completed",
    "exit_code": 1,
    "duration_ms": 850,
    "stdout": "collected 1 item\n\ntests/test_paginate.py F\n\n1 failed in 0.03s\n",
    "stderr": "",
}


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


async def test_list_experiments_of_an_investigation(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    first = (await client.post("/experiments", json=experiment_request(hypothesis["id"]))).json()
    second = (await client.post("/experiments", json=experiment_request(hypothesis["id"], command="pytest -q"))).json()

    resp = await client.get("/experiments", params={"investigation_id": hypothesis["investigation_id"]})
    other = await client.get("/experiments", params={"investigation_id": str(uuid.uuid4())})

    assert resp.status_code == 200
    assert [x["id"] for x in resp.json()] == [first["id"], second["id"]]
    assert other.status_code == 404


async def test_get_unknown_experiment_returns_404(client: AsyncClient) -> None:
    resp = await client.get(f"/experiments/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "experiment_not_found"


# --- approval: the human decides, agents never can ----------------------------------------------


@pytest.fixture
async def experiment(client: AsyncClient, hypothesis: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post("/experiments", json=experiment_request(hypothesis["id"]))
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def approved(client: AsyncClient, experiment: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(f"/experiments/{experiment['id']}/approve", headers=HUMAN)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("role", [Role.AGENT, Role.VALIDATOR])
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_only_the_human_can_decide(
    client: AsyncClient, experiment: dict[str, Any], role: Role, action: str
) -> None:
    resp = await client.post(f"/experiments/{experiment['id']}/{action}", headers=bearer(role))

    assert resp.status_code == 403
    assert resp.json()["reason_code"] == "role_not_allowed"
    assert (await client.get(f"/experiments/{experiment['id']}")).json()["status"] == "requested"


async def test_human_approval_records_the_approver(approved: dict[str, Any]) -> None:
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "human"


async def test_rejection_records_who_decided(client: AsyncClient, experiment: dict[str, Any]) -> None:
    resp = await client.post(f"/experiments/{experiment['id']}/reject", headers=HUMAN)

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["approved_by"] == "human"


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_decided_experiment_cannot_be_decided_again(
    client: AsyncClient, approved: dict[str, Any], action: str
) -> None:
    resp = await client.post(f"/experiments/{approved['id']}/{action}", headers=HUMAN)

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "experiment_not_requested"


async def test_approving_unknown_experiment_returns_404(client: AsyncClient) -> None:
    resp = await client.post(f"/experiments/{uuid.uuid4()}/approve", headers=HUMAN)

    assert resp.status_code == 404


# --- results: only for approved experiments -------------------------------------------------------


async def test_result_without_approval_is_refused(client: AsyncClient, experiment: dict[str, Any]) -> None:
    resp = await client.post(f"/experiments/{experiment['id']}/result", json=FAILING_RUN)

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "experiment_not_approved"


async def test_result_for_rejected_experiment_is_refused(client: AsyncClient, experiment: dict[str, Any]) -> None:
    await client.post(f"/experiments/{experiment['id']}/reject", headers=HUMAN)

    resp = await client.post(f"/experiments/{experiment['id']}/result", json=FAILING_RUN)

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "experiment_not_approved"


async def test_result_is_stored_with_logs_on_disk(client: AsyncClient, approved: dict[str, Any]) -> None:
    resp = await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN | {"stderr": "warn\n"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["exit_code"] == 1
    assert body["duration_ms"] == 850
    assert body["finished_at"] is not None
    expected_dir = f"artifacts/{approved['investigation_id']}/{approved['id']}"
    assert body["stdout_path"] == f"{expected_dir}/stdout.log"
    assert body["stderr_path"] == f"{expected_dir}/stderr.log"
    folder = settings.artifacts_dir / approved["investigation_id"] / approved["id"]
    assert (folder / "stdout.log").read_text(encoding="utf-8") == FAILING_RUN["stdout"]
    assert (folder / "stderr.log").read_text(encoding="utf-8") == "warn\n"


async def test_result_creates_experiment_output_evidence(client: AsyncClient, approved: dict[str, Any]) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN)

    resp = await client.get("/evidence", params={"investigation_id": approved["investigation_id"]})

    [evidence] = resp.json()
    assert evidence["kind"] == "observation"
    assert evidence["source_type"] == "experiment_output"
    assert evidence["experiment_id"] == approved["id"]
    assert evidence["hypothesis_id"] == approved["hypothesis_id"]
    assert evidence["snippet"].endswith("1 failed in 0.03s")
    assert "exit code 1" in evidence["note"]
    assert evidence["repo_sha"] is not None


async def test_second_result_is_refused(client: AsyncClient, approved: dict[str, Any]) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN)

    resp = await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN | {"exit_code": 0})

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "experiment_not_approved"
    assert (await client.get(f"/experiments/{approved['id']}")).json()["exit_code"] == 1


async def test_timed_out_result_has_no_exit_code(client: AsyncClient, approved: dict[str, Any]) -> None:
    run = {"status": "timed_out", "duration_ms": 60_000, "stdout": "", "stderr": ""}

    resp = await client.post(f"/experiments/{approved['id']}/result", json=run)

    assert resp.status_code == 200
    assert resp.json()["status"] == "timed_out"
    assert resp.json()["exit_code"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"status": "running"},
        {"exit_code": None},
        {"duration_ms": -1},
        {"stdout": "x" * (MAX_LOG_CHARS + 1)},
        {"outcome": "confirms"},
    ],
    ids=[
        "status-approved",
        "status-running",
        "completed-without-exit-code",
        "negative-duration",
        "huge-log",
        "outcome",
    ],
)
async def test_invalid_result_rejected(
    client: AsyncClient, approved: dict[str, Any], overrides: dict[str, Any]
) -> None:
    resp = await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN | overrides)

    assert resp.status_code == 422


async def test_result_with_nul_bytes_is_stored(client: AsyncClient, approved: dict[str, Any]) -> None:
    resp = await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN | {"stdout": "bad\x00byte"})

    assert resp.status_code == 200, resp.text
    evidence = (await client.get("/evidence", params={"investigation_id": approved["investigation_id"]})).json()
    assert evidence[0]["snippet"] == "badbyte"  # Postgres text cannot hold NUL; the log file keeps it


# --- conclusion ---------------------------------------------------------------------------------


async def test_conclude_before_result_is_refused(client: AsyncClient, approved: dict[str, Any]) -> None:
    resp = await client.post(f"/experiments/{approved['id']}/conclude", json={"outcome": "confirms"}, headers=VALIDATOR)

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "experiment_has_no_result"


async def test_conclude_sets_outcome_once(client: AsyncClient, approved: dict[str, Any]) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN)

    first = await client.post(
        f"/experiments/{approved['id']}/conclude", json={"outcome": "confirms"}, headers=VALIDATOR
    )
    second = await client.post(
        f"/experiments/{approved['id']}/conclude", json={"outcome": "refutes"}, headers=VALIDATOR
    )

    assert first.status_code == 200
    assert first.json()["outcome"] == "confirms"
    assert second.status_code == 409
    assert second.json()["reason_code"] == "experiment_already_concluded"


async def test_agent_cannot_conclude_its_own_experiment(client: AsyncClient, approved: dict[str, Any]) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN)

    resp = await client.post(f"/experiments/{approved['id']}/conclude", json={"outcome": "confirms"})

    assert resp.status_code == 403
    assert resp.json()["reason_code"] == "role_not_allowed"
    assert (await client.get(f"/experiments/{approved['id']}")).json()["outcome"] is None


async def test_human_can_conclude(client: AsyncClient, approved: dict[str, Any]) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN)

    resp = await client.post(f"/experiments/{approved['id']}/conclude", json={"outcome": "refutes"}, headers=HUMAN)

    assert resp.status_code == 200


@pytest.mark.parametrize("outcome", ["confirms", "refutes"])
async def test_timed_out_run_can_only_be_inconclusive(
    client: AsyncClient, approved: dict[str, Any], outcome: str
) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json={"status": "timed_out", "duration_ms": 60_000})

    refused = await client.post(f"/experiments/{approved['id']}/conclude", json={"outcome": outcome}, headers=VALIDATOR)
    allowed = await client.post(
        f"/experiments/{approved['id']}/conclude", json={"outcome": "inconclusive"}, headers=VALIDATOR
    )

    assert refused.status_code == 409
    assert refused.json()["reason_code"] == "experiment_not_completed"
    assert allowed.status_code == 200


async def test_conclude_rejects_unknown_outcome(client: AsyncClient, approved: dict[str, Any]) -> None:
    await client.post(f"/experiments/{approved['id']}/result", json=FAILING_RUN)

    resp = await client.post(f"/experiments/{approved['id']}/conclude", json={"outcome": "probably"}, headers=VALIDATOR)

    assert resp.status_code == 422


# --- the verify guard, end to end through the experiment flow --------------------------------------


async def transition(client: AsyncClient, hypothesis_id: str, to: str, **kwargs: Any) -> Any:
    return await client.post(f"/hypotheses/{hypothesis_id}/transition", json={"to": to, "reason": "test"}, **kwargs)


async def supported_hypothesis(client: AsyncClient, hypothesis: dict[str, Any]) -> str:
    hyp = hypothesis["id"]
    evidence = await client.post(
        "/evidence",
        json={
            "investigation_id": hypothesis["investigation_id"], "hypothesis_id": hyp, "kind": "supporting",
            "source_type": "code_ref", "file_path": "pkg/paginate.py", "line_start": 3, "line_end": 4,
        },
    )  # fmt: skip
    assert evidence.status_code == 201, evidence.text
    assert (await transition(client, hyp, "under_review")).status_code == 200
    assert (await transition(client, hyp, "supported")).status_code == 200
    return hyp


async def test_verified_only_after_approved_run_confirms(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    hyp = await supported_hypothesis(client, hypothesis)
    exp = (await client.post("/experiments", json=experiment_request(hyp))).json()

    # Approved but not run: nothing to verify with yet.
    await client.post(f"/experiments/{exp['id']}/approve", headers=HUMAN)
    assert (await transition(client, hyp, "verified", headers=VALIDATOR)).json()["reason_code"] == (
        "no_confirming_experiment"
    )
    # Run but not concluded: still nothing.
    await client.post(f"/experiments/{exp['id']}/result", json=FAILING_RUN)
    assert (await transition(client, hyp, "verified", headers=VALIDATOR)).json()["reason_code"] == (
        "no_confirming_experiment"
    )

    await client.post(f"/experiments/{exp['id']}/conclude", json={"outcome": "confirms"}, headers=VALIDATOR)
    by_agent = await transition(client, hyp, "verified")
    by_validator = await transition(client, hyp, "verified", headers=VALIDATOR)

    assert by_agent.status_code == 409
    assert by_agent.json()["reason_code"] == "role_not_allowed"
    assert by_validator.status_code == 200
    assert by_validator.json()["status"] == "verified"


async def test_refuting_run_does_not_verify(client: AsyncClient, hypothesis: dict[str, Any]) -> None:
    hyp = await supported_hypothesis(client, hypothesis)
    exp = (await client.post("/experiments", json=experiment_request(hyp))).json()
    await client.post(f"/experiments/{exp['id']}/approve", headers=HUMAN)
    await client.post(f"/experiments/{exp['id']}/result", json=FAILING_RUN | {"exit_code": 0})
    await client.post(f"/experiments/{exp['id']}/conclude", json={"outcome": "refutes"}, headers=VALIDATOR)

    resp = await transition(client, hyp, "verified", headers=VALIDATOR)

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "no_confirming_experiment"
