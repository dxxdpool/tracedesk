"""Investigation cloning, the read-only repo endpoints and citation checks on POST /evidence."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models import Investigation
from app.routes.investigations import get_cloner
from app.main import app
from tests.conftest import FixtureRepo, git


def cite(investigation: dict[str, Any], **fields: Any) -> dict[str, Any]:
    base = {"investigation_id": investigation["id"], "kind": "supporting", "source_type": "code_ref"}
    return base | fields


# --- POST /investigations clones and pins ------------------------------------------------------


async def test_create_investigation_clones_and_pins_head(
    investigation: dict[str, Any], fixture_repo: FixtureRepo, workspace_settings: Path
) -> None:
    repo_dir = workspace_settings / investigation["id"] / "repo"

    assert investigation["repo_sha"] == fixture_repo.head
    assert investigation["workspace_path"] == str(repo_dir)
    assert (repo_dir / "pkg" / "paginate.py").is_file()


async def test_workspace_path_uses_host_dir_when_set(
    client: AsyncClient, investigation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_host_dir", "C:\\tracedesk\\workspaces")

    resp = await client.get(f"/investigations/{investigation['id']}")

    assert resp.json()["workspace_path"] == f"C:\\tracedesk\\workspaces\\{investigation['id']}\\repo"


@pytest.mark.parametrize(
    "url", ["https://gitlab.com/a/b", "https://user:pw@github.com/a/b", "file:///etc", "eval/cases/x"]
)
async def test_create_investigation_rejects_bad_repo_url_without_a_row(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], url: str
) -> None:
    resp = await client.post("/investigations", json={"repo_url": url, "problem": "bug"})

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "repo_url_invalid"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Investigation)) == 0


async def test_failed_clone_returns_422_and_stores_nothing(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], workspace_settings: Path
) -> None:
    from app.services.workspace import WorkspaceError

    def failing_clone(*_args: object) -> str:
        raise WorkspaceError("clone_failed", "git clone failed: repository not found")

    app.dependency_overrides[get_cloner] = lambda: failing_clone

    resp = await client.post("/investigations", json={"repo_url": "https://github.com/a/missing", "problem": "p"})

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "clone_failed"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Investigation)) == 0


async def test_local_repo_under_cases_cloned_when_allowed(
    client: AsyncClient, fixture_repo: FixtureRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases"
    shutil.copytree(fixture_repo.path, cases / "seed_bug")
    monkeypatch.setattr(settings, "allow_local_repos", True)
    app.dependency_overrides.pop(get_cloner)  # the real cloner, on the real local path

    resp = await client.post("/investigations", json={"repo_url": "eval/cases/seed_bug", "problem": "p"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["repo_sha"] == fixture_repo.head


# --- read-only repo views ----------------------------------------------------------------------


async def test_files_endpoint_lists_pinned_tree(client: AsyncClient, investigation: dict[str, Any]) -> None:
    resp = await client.get(f"/investigations/{investigation['id']}/files")

    assert resp.status_code == 200
    assert "pkg/paginate.py" in resp.json()["files"]
    assert resp.json()["repo_sha"] == investigation["repo_sha"]


async def test_file_endpoint_returns_lines(client: AsyncClient, investigation: dict[str, Any]) -> None:
    resp = await client.get(
        f"/investigations/{investigation['id']}/file", params={"path": "pkg/paginate.py", "start": 2, "end": 3}
    )

    assert resp.status_code == 200
    assert resp.json()["lines"] == ["# line 2", "# line 3"]
    assert resp.json()["line_count"] == 30


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [({"path": "nope.py"}, 404, "file_not_found"), ({"path": "../../etc/passwd"}, 422, "path_invalid"),
     ({"path": "pkg/paginate.py", "start": 29, "end": 31}, 422, "lines_out_of_range")],
)  # fmt: skip
async def test_file_endpoint_rejections(
    client: AsyncClient, investigation: dict[str, Any], params: dict[str, Any], status: int, code: str
) -> None:
    resp = await client.get(f"/investigations/{investigation['id']}/file", params=params)

    assert resp.status_code == status
    assert resp.json()["reason_code"] == code


async def test_log_and_blame_endpoints(
    client: AsyncClient, investigation: dict[str, Any], fixture_repo: FixtureRepo
) -> None:
    inv = investigation["id"]

    log = await client.get(f"/investigations/{inv}/log", params={"path": "pkg/paginate.py"})
    blame = await client.get(f"/investigations/{inv}/blame", params={"path": "pkg/paginate.py", "start": 1, "end": 1})

    assert [c["sha"] for c in log.json()] == [fixture_repo.commits[0]]
    assert blame.json()[0]["commit"] == fixture_repo.commits[0]


async def test_repo_views_need_a_token(client: AsyncClient, investigation: dict[str, Any]) -> None:
    resp = await client.get(f"/investigations/{investigation['id']}/files", headers={"Authorization": ""})

    assert resp.status_code == 401


# --- POST /evidence citations ------------------------------------------------------------------


async def test_code_ref_snippet_and_sha_captured_by_server(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    resp = await client.post(
        "/evidence", json=cite(investigation, file_path="./pkg\\paginate.py", line_start=7, line_end=8)
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["snippet"] == "# line 7\n# line 8"
    assert body["repo_sha"] == investigation["repo_sha"]
    assert body["file_path"] == "pkg/paginate.py"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"file_path": "pkg/invented.py", "line_start": 1, "line_end": 2}, "file_not_found"),
        ({"file_path": "pkg/paginate.py", "line_start": 25, "line_end": 31}, "lines_out_of_range"),
        ({"file_path": "pkg/paginate.py", "line_start": 31, "line_end": 31}, "lines_out_of_range"),
        ({"file_path": "../outside.py", "line_start": 1, "line_end": 1}, "path_invalid"),
        ({"file_path": "/etc/passwd", "line_start": 1, "line_end": 1}, "path_invalid"),
        ({"file_path": "C:/Windows/win.ini", "line_start": 1, "line_end": 1}, "path_invalid"),
        ({"file_path": "escape_link", "line_start": 1, "line_end": 1}, "file_is_symlink"),
        ({"file_path": "pkg", "line_start": 1, "line_end": 1}, "not_a_file"),
        ({"file_path": "data.bin", "line_start": 1, "line_end": 1}, "file_not_text"),
    ],
    ids=["missing-file", "past-end", "start-past-end", "dotdot", "absolute", "drive", "symlink", "dir", "binary"],
)
async def test_fabricated_code_ref_rejected_with_reason(
    client: AsyncClient, investigation: dict[str, Any], fields: dict[str, Any], code: str
) -> None:
    resp = await client.post("/evidence", json=cite(investigation, **fields))

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == code


@pytest.mark.parametrize(
    "fields",
    [
        {"file_path": "pkg/paginate.py"},
        {"line_start": 1, "line_end": 2},
        {"file_path": "pkg/paginate.py", "line_start": 1, "line_end": 2, "commit_sha": "abcd"},
        {"source_type": "git_history"},
        {"source_type": "git_history", "commit_sha": "abcd", "line_start": 1, "line_end": 2},
        {"source_type": "git_history", "commit_sha": "HEAD~1"},
        {"source_type": "human_note", "file_path": "pkg/paginate.py", "line_start": 1, "line_end": 1},
        {"source_type": "human_note", "commit_sha": "abcd"},
        {"source_type": "experiment_output", "file_path": "pkg/paginate.py"},
    ],
    ids=[
        "code-ref-no-lines", "code-ref-no-file", "code-ref-with-commit", "history-no-commit",
        "history-with-lines", "history-ref-not-sha", "note-with-citation", "note-with-commit",
        "output-with-file",
    ],
)
async def test_citation_shape_enforced_per_source(
    client: AsyncClient, investigation: dict[str, Any], fields: dict[str, Any]
) -> None:
    resp = await client.post("/evidence", json=cite(investigation, **fields))

    assert resp.status_code == 422


async def test_citation_reads_pinned_commit_after_the_clone_changes(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    repo_dir = Path(investigation["workspace_path"])
    (repo_dir / "pkg" / "paginate.py").write_text("tampered\n")
    git(repo_dir, "commit", "--quiet", "-am", "tamper")

    resp = await client.post(
        "/evidence", json=cite(investigation, file_path="pkg/paginate.py", line_start=1, line_end=1)
    )

    assert resp.status_code == 201
    assert resp.json()["snippet"] == "# line 1"


async def test_code_ref_on_unpinned_investigation_rejected(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        legacy = Investigation(repo_url="https://github.com/a/b", problem="from before Day 2")
        session.add(legacy)
        await session.commit()
        legacy_id = str(legacy.id)

    resp = await client.post(
        "/evidence", json=cite({"id": legacy_id}, file_path="pkg/paginate.py", line_start=1, line_end=1)
    )

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == "investigation_not_pinned"


async def test_git_history_stores_full_sha_and_subject(
    client: AsyncClient, investigation: dict[str, Any], fixture_repo: FixtureRepo
) -> None:
    first = fixture_repo.commits[0]

    resp = await client.post(
        "/evidence",
        json=cite(investigation, source_type="git_history", commit_sha=first[:10], file_path="pkg/paginate.py"),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit_sha"] == first
    assert body["snippet"] == "add paginate helper"
    assert body["file_path"] == "pkg/paginate.py"
    assert body["repo_sha"] == investigation["repo_sha"]


@pytest.mark.parametrize(
    ("fields", "code"),
    [({"commit_sha": "deadbeefdeadbeef"}, "commit_not_found"),
     ({"commit_sha": "COMMIT0", "file_path": "pkg/invented.py"}, "file_not_found")],
)  # fmt: skip
async def test_git_history_rejects_unknown_commit_or_file(
    client: AsyncClient, investigation: dict[str, Any], fixture_repo: FixtureRepo, fields: dict[str, Any], code: str
) -> None:
    if fields["commit_sha"] == "COMMIT0":
        fields = fields | {"commit_sha": fixture_repo.commits[0]}

    resp = await client.post("/evidence", json=cite(investigation, source_type="git_history", **fields))

    assert resp.status_code == 422
    assert resp.json()["reason_code"] == code


async def test_failed_clone_leaves_no_workspace_folder(client: AsyncClient, workspace_settings: Path) -> None:
    from app.services.workspace import WorkspaceError

    def half_clone(_source: object, dest: Path) -> str:
        dest.mkdir(parents=True)
        raise WorkspaceError("clone_failed", "boom")

    app.dependency_overrides[get_cloner] = lambda: half_clone

    resp = await client.post("/investigations", json={"repo_url": "https://github.com/a/b", "problem": "p"})

    assert resp.status_code == 422
    assert not workspace_settings.exists() or not any(workspace_settings.iterdir())


async def test_long_lines_truncated_in_snippet(
    client: AsyncClient, investigation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import citations

    monkeypatch.setattr(citations, "MAX_SNIPPET_LINE_CHARS", 4)

    resp = await client.post(
        "/evidence", json=cite(investigation, file_path="pkg/paginate.py", line_start=1, line_end=1)
    )

    assert resp.json()["snippet"] == "# li" + citations.TRUNCATED
