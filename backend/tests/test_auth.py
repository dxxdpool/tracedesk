import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.config import Settings
from app.state_machine import Role
from tests.conftest import bearer

PROTECTED = [
    ("POST", "/investigations"),
    ("GET", "/investigations/00000000-0000-0000-0000-000000000000"),
    ("GET", "/hypotheses?investigation_id=00000000-0000-0000-0000-000000000000"),
    ("POST", "/hypotheses/00000000-0000-0000-0000-000000000000/transition"),
    ("GET", "/hypotheses/00000000-0000-0000-0000-000000000000/events"),
    ("POST", "/evidence"),
    ("POST", "/experiments"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_missing_token_rejected(client: AsyncClient, method: str, path: str) -> None:
    client.headers.pop("Authorization")
    resp = await client.request(method, path, json={})
    assert resp.status_code == 401
    assert resp.json()["reason_code"] == "token_missing"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_wrong_token_rejected(client: AsyncClient, method: str, path: str) -> None:
    resp = await client.request(method, path, json={}, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["reason_code"] == "token_invalid"


async def test_non_bearer_scheme_rejected(client: AsyncClient) -> None:
    resp = await client.post("/investigations", json={}, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


@pytest.mark.parametrize("role", list(Role))
async def test_every_role_token_accepted(client: AsyncClient, role: Role) -> None:
    resp = await client.post(
        "/investigations",
        json={"repo_url": "https://github.com/example/repo", "problem": "bug"},
        headers=bearer(role),
    )
    assert resp.status_code == 201


async def test_health_needs_no_token(client: AsyncClient) -> None:
    client.headers.pop("Authorization")
    assert (await client.get("/health")).status_code == 200


def _settings(**tokens: str) -> Settings:
    return Settings(database_url="postgresql+asyncpg://x@localhost/x", _env_file=None, **tokens)


def test_settings_reject_shared_tokens() -> None:
    same = "a" * 32
    with pytest.raises(ValidationError, match="must all be different"):
        _settings(agent_token=same, validator_token=same, approver_token="b" * 32)


def test_settings_reject_short_tokens_without_echoing_them() -> None:
    with pytest.raises(ValidationError, match="at least 16") as exc_info:
        _settings(agent_token="short-secret", validator_token="v" * 32, approver_token="b" * 32)
    assert "short-secret" not in str(exc_info.value)


def test_settings_reject_placeholder_tokens() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(agent_token="change-me-agent-token", validator_token="v" * 32, approver_token="b" * 32)
