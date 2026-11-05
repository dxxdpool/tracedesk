"""Test setup: a separate `tracedesk_test` database, rebuilt from the Alembic migrations
once per run and emptied after every test."""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
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
from app.routes.investigations import get_cloner
from app.services import workspace
from app.services.workspace import RepoSource
from app.state_machine import Role

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "tracedesk_test"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or (
    make_url(settings.database_url).set(database=TEST_DB_NAME).render_as_string(hide_password=False)
)
ACTOR = "tester"
PAGINATE_LINES = 30


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


def git(repo: Path, *args: str) -> str:
    """Run git in a test repo (never in the app's workspaces)."""
    result = subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "core.autocrlf=false",
         "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return result.stdout.strip()


def make_fixture_repo(repo: Path) -> list[str]:
    """A small repo with three commits; returns their SHAs, oldest first."""
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "paginate.py").write_text("".join(f"# line {n}\n" for n in range(1, PAGINATE_LINES + 1)))
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "add paginate helper")
    (repo / "README.md").write_text("# fixture\n")
    (repo / "data.bin").write_bytes(b"\x00\x01binary")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "add readme and data")
    # A symlink pointing outside the repo, added to the index directly so no OS symlink support is needed.
    link_blob = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="../../outside", capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{link_blob},escape_link")
    git(repo, "commit", "--quiet", "-m", "add a symlink")
    return git(repo, "rev-list", "--reverse", "HEAD").split()


@dataclass(frozen=True)
class FixtureRepo:
    path: Path
    commits: list[str]

    @property
    def head(self) -> str:
        return self.commits[-1]


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepo:
    path = tmp_path_factory.mktemp("fixture") / "repo"
    return FixtureRepo(path, make_fixture_repo(path))


@pytest.fixture(autouse=True)
def workspace_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own temporary workspaces and artifacts folders; local repos off by default."""
    workspaces = tmp_path / "workspaces"
    monkeypatch.setattr(settings, "workspaces_dir", workspaces)
    monkeypatch.setattr(settings, "workspaces_host_dir", None)
    monkeypatch.setattr(settings, "allow_local_repos", False)
    monkeypatch.setattr(settings, "local_repos_dir", tmp_path / "cases")
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path / "artifacts")
    return workspaces


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], fixture_repo: FixtureRepo
) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    def clone_fixture(_source: RepoSource, dest: Path) -> str:
        # URL validation still ran for real; only the network clone is swapped for the local fixture.
        return workspace.clone(RepoSource(fixture_repo.path.as_uri(), is_local=True), dest)

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_cloner] = lambda: clone_fixture
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
