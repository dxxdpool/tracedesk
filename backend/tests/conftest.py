"""Test setup: a separate `tracedesk_test` database, rebuilt from the Alembic migrations
once per run and emptied after every test."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import Base
from app.state_machine import Role

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "tracedesk_test"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or (
    make_url(settings.database_url).set(database=TEST_DB_NAME).render_as_string(hide_password=False)
)
ACTOR = "tester"


def bearer(role: Role) -> dict[str, str]:
    """Authorization header for a role, using the tokens the app itself loaded."""
    token = {
        Role.AGENT: settings.agent_token,
        Role.VALIDATOR: settings.validator_token,
        Role.HUMAN: settings.approver_token,
    }[role]
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


async def _recreate_test_database() -> None:
    admin_url = make_url(TEST_DATABASE_URL).set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Fresh database per run, built by the real migrations (so they are tested too)."""
    asyncio.run(_recreate_test_database())
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")
    yield


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # A new engine per test: asyncpg connections belong to one event loop, and each test has its own.
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    await engine.dispose()


@pytest.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    # Default identity: an agent. Tests pass headers=bearer(Role.X) to act as another role.
    headers = {"X-Actor": ACTOR, **bearer(Role.AGENT)}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def investigation(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post(
        "/investigations",
        json={"repo_url": "https://github.com/example/repo", "problem": "page 1 skips the first item"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def hypothesis(client: AsyncClient, investigation: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(
        "/hypotheses",
        json={"investigation_id": investigation["id"], "text": "start index is off by one"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
