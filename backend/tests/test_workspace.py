"""services/workspace.py: repo URL validation, cloning, path safety and reads at the pinned SHA.

No network: clones come from the local fixture repo (tests/conftest.py).
"""

import os
from pathlib import Path

import pytest

from app.services import workspace
from app.services.workspace import RepoSource, Workspace, WorkspaceError
from tests.conftest import PAGINATE_LINES, FixtureRepo, git


@pytest.fixture
def ws(fixture_repo: FixtureRepo, tmp_path: Path) -> Workspace:
    dest = tmp_path / "ws" / "repo"
    sha = workspace.clone(RepoSource(fixture_repo.path.as_uri(), is_local=True), dest)
    return Workspace(dest, sha)


def reason(exc: pytest.ExceptionInfo[WorkspaceError]) -> str:
    return exc.value.reason_code


# --- repo URLs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "clone_url"),
    [
        ("https://github.com/octocat/Hello-World", "https://github.com/octocat/Hello-World.git"),
        ("https://github.com/octocat/Hello-World.git", "https://github.com/octocat/Hello-World.git"),
        ("https://github.com/octocat/Hello-World/", "https://github.com/octocat/Hello-World.git"),
        ("  https://github.com/a/b.c_d-e  ", "https://github.com/a/b.c_d-e.git"),
    ],
)
def test_github_urls_accepted(url: str, clone_url: str, tmp_path: Path) -> None:
    source = workspace.parse_repo_url(url, allow_local=False, local_dir=tmp_path)

    assert source == RepoSource(clone_url, is_local=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/octocat/Hello-World",
        "https://gitlab.com/octocat/Hello-World",
        "https://github.com.evil.com/octocat/Hello-World",
        "https://evil.com/github.com/octocat/Hello-World",
        "https://user:pass@github.com/octocat/Hello-World",
        "https://token@github.com/octocat/Hello-World",
        "https://github.com:443/octocat/Hello-World",
        "https://github.com/octocat",
        "https://github.com/octocat/Hello-World/tree/main",
        "https://github.com/octocat/Hello-World?x=1",
        "https://github.com/octocat/Hello-World#frag",
        "https://github.com/-octocat/repo",
        "https://github.com/octocat/..",
        "https://github.com/../etc",
        "https://GITHUB.com/octocat/Hello-World",
        "git@github.com:octocat/Hello-World.git",
        "ssh://git@github.com/octocat/Hello-World",
        "git://github.com/octocat/Hello-World",
        "file:///etc",
        "ext::sh -c touch% /tmp/pwned",
        "--upload-pack=touch /tmp/pwned",
        "/etc",
        "eval/cases/x",
        "https://github.com/octocat/Hello World",
        "https://github.com/octocat/Hello-World\nhttps://evil.com/a/b",
    ],
)
def test_other_urls_rejected(url: str, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.parse_repo_url(url, allow_local=False, local_dir=tmp_path)

    assert reason(exc) == "repo_url_invalid"


def test_local_repo_accepted_only_when_allowed(fixture_repo: FixtureRepo) -> None:
    cases = fixture_repo.path.parent

    source = workspace.parse_repo_url("eval/cases/repo", allow_local=True, local_dir=cases)

    assert source == RepoSource(fixture_repo.path.resolve().as_uri(), is_local=True)
    assert workspace.parse_repo_url(str(fixture_repo.path), allow_local=True, local_dir=cases).is_local


@pytest.mark.parametrize("value", ["eval/cases/../..", "eval/cases/", "..", "missing"])
def test_local_repo_outside_cases_or_not_a_repo_rejected(fixture_repo: FixtureRepo, value: str) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.parse_repo_url(value, allow_local=True, local_dir=fixture_repo.path.parent)

    assert reason(exc) == "local_repo_invalid"


def test_local_absolute_path_outside_cases_rejected(fixture_repo: FixtureRepo, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.parse_repo_url(str(fixture_repo.path), allow_local=True, local_dir=tmp_path)

    assert reason(exc) == "local_repo_invalid"


# --- cloning -----------------------------------------------------------------------------------


def test_clone_pins_exact_head_sha_and_disables_hooks(ws: Workspace, fixture_repo: FixtureRepo) -> None:
    assert ws.sha == fixture_repo.head
    assert git(ws.repo_dir, "config", "core.hooksPath") == os.devnull
    hooks = ws.repo_dir / ".git" / "hooks"
    assert not hooks.exists() or not any(hooks.iterdir())  # no template hooks copied in


def test_clone_checks_out_symlinks_as_plain_files(ws: Workspace) -> None:
    link = ws.repo_dir / "escape_link"

    assert not link.is_symlink()
    assert link.read_text() == "../../outside"


def test_clone_failure_reports_reason_and_leaves_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "ws" / "repo"

    with pytest.raises(WorkspaceError) as exc:
        workspace.clone(RepoSource((tmp_path / "nope").as_uri(), is_local=True), dest)

    assert reason(exc) == "clone_failed"
    assert not dest.exists()


def test_file_protocol_refused_for_non_local_sources(fixture_repo: FixtureRepo, tmp_path: Path) -> None:
    # A GitHub source must never be able to fetch from the local disk.
    with pytest.raises(WorkspaceError) as exc:
        workspace.clone(RepoSource(fixture_repo.path.as_uri(), is_local=False), tmp_path / "repo")

    assert reason(exc) == "clone_failed"


def test_clone_over_size_cap_rejected(
    fixture_repo: FixtureRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace, "MAX_REPO_BYTES", 10)
    dest = tmp_path / "repo"

    with pytest.raises(WorkspaceError) as exc:
        workspace.clone(RepoSource(fixture_repo.path.as_uri(), is_local=True), dest)

    assert reason(exc) == "repo_too_large"
    assert not dest.exists()


def test_clone_timeout_rejected(fixture_repo: FixtureRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, "CLONE_TIMEOUT_SECONDS", -1)
    monkeypatch.setattr(workspace, "SIZE_POLL_SECONDS", 0.0)

    with pytest.raises(WorkspaceError) as exc:
        workspace.clone(RepoSource(fixture_repo.path.as_uri(), is_local=True), tmp_path / "repo")

    assert reason(exc) == "clone_timeout"


# --- paths -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("pkg/paginate.py", "pkg/paginate.py"), ("./pkg//paginate.py", "pkg/paginate.py"),
     ("pkg\\paginate.py", "pkg/paginate.py"), (" pkg/paginate.py ", "pkg/paginate.py")],
)  # fmt: skip
def test_repo_paths_normalized(raw: str, expected: str) -> None:
    assert workspace.normalize_repo_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["../etc/passwd", "pkg/../../etc/passwd", "/etc/passwd", "C:/Windows/win.ini", "C:\\Windows\\win.ini",
     "\\\\server\\share", "..", ".", "", "   ", ".git/config", "pkg/\x00.py"],
)  # fmt: skip
def test_traversal_and_absolute_paths_rejected(raw: str) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.normalize_repo_path(raw)

    assert reason(exc) == "path_invalid"


def test_safe_path_rejects_symlink_escape_on_disk(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, ws.repo_dir / "planted", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this OS")

    with pytest.raises(WorkspaceError) as exc:
        workspace.safe_path(ws, "planted/secret.txt")

    assert reason(exc) == "path_escapes_workspace"


def test_safe_path_returns_path_inside_checkout(ws: Workspace) -> None:
    assert workspace.safe_path(ws, "pkg/paginate.py") == (ws.repo_dir / "pkg" / "paginate.py").resolve()


# --- reads at the pinned commit ----------------------------------------------------------------


def test_read_lines_returns_inclusive_range(ws: Workspace) -> None:
    path, lines = workspace.read_lines(ws, "pkg/paginate.py", 3, 5)

    assert path == "pkg/paginate.py"
    assert lines == ["# line 3", "# line 4", "# line 5"]


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [(1, PAGINATE_LINES + 1, "lines_out_of_range"), (PAGINATE_LINES + 1, PAGINATE_LINES + 1, "lines_out_of_range"),
     (5, 4, "lines_invalid"), (0, 2, "lines_invalid")],
)  # fmt: skip
def test_read_lines_rejects_bad_ranges(ws: Workspace, start: int, end: int, code: str) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.read_lines(ws, "pkg/paginate.py", start, end)

    assert reason(exc) == code


def test_read_lines_rejects_too_long_range(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, "MAX_RANGE_LINES", 2)

    with pytest.raises(WorkspaceError) as exc:
        workspace.read_lines(ws, "pkg/paginate.py", 1, 3)

    assert reason(exc) == "range_too_long"


@pytest.mark.parametrize(
    ("path", "code"),
    [("pkg/missing.py", "file_not_found"), ("pkg", "not_a_file"), ("escape_link", "file_is_symlink"),
     ("data.bin", "file_not_text")],
)  # fmt: skip
def test_read_file_rejects_what_cannot_be_cited(ws: Workspace, path: str, code: str) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.read_file(ws, path)

    assert reason(exc) == code


def test_read_file_rejects_large_file(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, "MAX_FILE_BYTES", 10)

    with pytest.raises(WorkspaceError) as exc:
        workspace.read_file(ws, "pkg/paginate.py")

    assert reason(exc) == "file_too_large"


def test_reads_stay_pinned_when_the_clone_changes_later(ws: Workspace) -> None:
    target = ws.repo_dir / "pkg" / "paginate.py"
    target.write_text("rewritten\n")
    (ws.repo_dir / "pkg" / "new.py").write_text("x = 1\n")
    git(ws.repo_dir, "add", ".")
    git(ws.repo_dir, "commit", "--quiet", "-m", "later change")

    _path, lines = workspace.read_lines(ws, "pkg/paginate.py", 1, 2)
    with pytest.raises(WorkspaceError) as exc:
        workspace.read_file(ws, "pkg/new.py")

    assert lines == ["# line 1", "# line 2"]
    assert reason(exc) == "file_not_found"
    assert "pkg/new.py" not in workspace.list_files(ws)[0]


def test_missing_workspace_reported(ws: Workspace, tmp_path: Path) -> None:
    gone = Workspace(tmp_path / "gone", ws.sha)

    with pytest.raises(WorkspaceError) as exc:
        workspace.read_file(gone, "pkg/paginate.py")

    assert reason(exc) == "workspace_missing"


def test_list_files_at_pin(ws: Workspace) -> None:
    files, truncated = workspace.list_files(ws)

    assert set(files) == {"pkg/__init__.py", "pkg/paginate.py", "README.md", "data.bin", "escape_link"}
    assert not truncated


def test_list_files_truncates(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, "MAX_LISTED_FILES", 2)

    files, truncated = workspace.list_files(ws)

    assert len(files) == 2
    assert truncated


def test_git_log_for_path(ws: Workspace, fixture_repo: FixtureRepo) -> None:
    everything = workspace.git_log(ws)
    for_file = workspace.git_log(ws, "pkg/paginate.py")

    assert [c.sha for c in everything] == list(reversed(fixture_repo.commits))
    assert [c.subject for c in for_file] == ["add paginate helper"]


def test_git_blame_names_introducing_commit(ws: Workspace, fixture_repo: FixtureRepo) -> None:
    blame = workspace.git_blame(ws, "pkg/paginate.py", 2, 3)

    assert [(b.line, b.commit, b.text) for b in blame] == [
        (2, fixture_repo.commits[0], "# line 2"),
        (3, fixture_repo.commits[0], "# line 3"),
    ]
    assert blame[0].summary == "add paginate helper"


def test_resolve_commit_accepts_short_sha(ws: Workspace, fixture_repo: FixtureRepo) -> None:
    info = workspace.resolve_commit(ws, fixture_repo.commits[0][:8])

    assert info.sha == fixture_repo.commits[0]
    assert info.subject == "add paginate helper"


@pytest.mark.parametrize(
    ("commit", "code"),
    [("deadbeef", "commit_not_found"), ("HEAD", "commit_invalid"), ("main", "commit_invalid"),
     ("--all", "commit_invalid")],
)  # fmt: skip
def test_resolve_commit_rejects_unknown_or_non_sha(ws: Workspace, commit: str, code: str) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.resolve_commit(ws, commit)

    assert reason(exc) == code


def test_resolve_commit_rejects_commit_after_the_pin(ws: Workspace) -> None:
    (ws.repo_dir / "later.txt").write_text("later\n")
    git(ws.repo_dir, "add", ".")
    git(ws.repo_dir, "commit", "--quiet", "-m", "after the pin")
    later = git(ws.repo_dir, "rev-parse", "HEAD")

    with pytest.raises(WorkspaceError) as exc:
        workspace.resolve_commit(ws, later)

    assert reason(exc) == "commit_not_in_history"


# --- hardening from the Day 2 Part A security review ------------------------------------------


def test_clone_persists_hardening_for_later_host_git(ws: Workspace) -> None:
    for key, value in workspace.PERSISTED_GIT_CONFIG.items():
        assert git(ws.repo_dir, "config", "--get", key) == value


@pytest.mark.parametrize("raw", [":(icase)PKG/PAGINATE.PY", ":(glob)pkg/*.py", "pkg/pag*.py"])
def test_pathspec_magic_does_not_match_another_file(ws: Workspace, raw: str) -> None:
    with pytest.raises(WorkspaceError) as exc:
        workspace.read_file(ws, raw)

    assert reason(exc) == "file_not_found"


def test_commit_text_cannot_forge_log_entries(ws: Workspace) -> None:
    forged = "real\x1e" + "f" * 40 + "\x1fmallory\x1f2020-01-01\x1fforged\x1b[31m"
    git(ws.repo_dir, "commit", "--quiet", "--allow-empty", "-m", forged)
    pinned = Workspace(ws.repo_dir, git(ws.repo_dir, "rev-parse", "HEAD"))

    newest = workspace.git_log(pinned)[0]
    info = workspace.resolve_commit(pinned, pinned.sha)

    assert newest.sha == pinned.sha
    assert info.subject == newest.subject
    assert "\x1b" not in newest.subject and "\x1e" not in newest.subject
    assert len(workspace.git_log(pinned)) == 4


def test_git_does_not_receive_api_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TOKEN", "secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")

    env = workspace._git_env()

    assert "AGENT_TOKEN" not in env and "DATABASE_URL" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_local_repo_with_git_file_rejected(tmp_path: Path) -> None:
    case = tmp_path / "cases" / "sneaky"
    case.mkdir(parents=True)
    (case / ".git").write_text("gitdir: /somewhere/else\n")

    with pytest.raises(WorkspaceError) as exc:
        workspace.parse_repo_url("eval/cases/sneaky", allow_local=True, local_dir=tmp_path / "cases")

    assert reason(exc) == "local_repo_invalid"
