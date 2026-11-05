"""The host-side sandbox runner (cli/st/sandbox.py).

Tests marked `docker` start real containers from the tracedesk-sandbox image and are skipped when
Docker or the image is missing (build it with `docker build -t tracedesk-sandbox sandbox/`).
"""

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from app.config import PROJECT_DIR
from app.services import workspace
from app.services.workspace import RepoSource, Workspace
from st import sandbox
from st.client import CliError
from tests.conftest import FixtureRepo

EXPERIMENT_ID = "6f1c2b9e-3d4a-4f5b-8c7d-0e1f2a3b4c5d"


def test_docker_command_applies_every_restriction(tmp_path: Path) -> None:
    argv = sandbox.docker_command(EXPERIMENT_ID, tmp_path, "pytest -q")

    joined = " ".join(argv)
    for flag in (
        "--rm", "--pull never", "--network none", "--read-only", "--cap-drop ALL",
        "--security-opt no-new-privileges", "--pids-limit 256", "--memory 512m", "--memory-swap 512m",
        "--cpus 1", "--user 1000:1000", "PYTEST_ADDOPTS=-p no:cacheprovider", "PYTHONDONTWRITEBYTECODE=1",
    ):  # fmt: skip
        assert flag in joined, flag
    assert f"type=bind,source={tmp_path.resolve()},target=/work,readonly" in argv
    assert f"st-exp-{EXPERIMENT_ID}" in argv
    # The approved command is one argument to sh inside the container, never parsed on the host.
    assert argv[-4:] == [sandbox.SANDBOX_IMAGE, "sh", "-c", "pytest -q"]
    assert "/var/run/docker.sock" not in joined


def test_container_name_requires_a_uuid() -> None:
    with pytest.raises(ValueError):
        sandbox.container_name("--privileged")


def test_workspace_path_with_comma_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CliError) as exc:
        sandbox.docker_command(EXPERIMENT_ID, tmp_path / "a,readonly=false", "true")
    assert exc.value.reason_code == "workspace_path_invalid"


def python_argv(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_execute_records_exit_code_and_output() -> None:
    result = sandbox.execute(
        python_argv("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), "x", 30
    )

    assert result.status == "completed"
    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.duration_ms >= 0


def test_execute_truncates_large_output() -> None:
    result = sandbox.execute(python_argv("import sys; sys.stdout.write('a' * 300_000)"), "x", 30)

    assert result.status == "completed"
    assert result.stdout.startswith("a" * sandbox.MAX_OUTPUT_BYTES)
    assert result.stdout.endswith(sandbox.TRUNCATED_MARKER)
    assert len(result.stdout) == sandbox.MAX_OUTPUT_BYTES + len(sandbox.TRUNCATED_MARKER)


def test_execute_times_out_and_kills_the_process() -> None:
    result = sandbox.execute(python_argv("import time; print('started', flush=True); time.sleep(60)"), "x", 1)

    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.duration_ms < 30_000
    assert "started" in result.stdout


def test_docker_run_error_maps_to_failed() -> None:
    result = sandbox.execute(python_argv(f"import sys; sys.exit({sandbox.DOCKER_RUN_FAILED})"), "x", 30)

    assert result.status == "failed"


def test_missing_program_is_reported() -> None:
    with pytest.raises(CliError) as exc:
        sandbox.execute(["definitely-not-docker-xyz"], "x", 5)
    assert exc.value.reason_code == "docker_missing"


# --- the workspace must match the pinned commit ---------------------------------------------------


@dataclass(frozen=True)
class Clone:
    repo: Path
    entries: list[dict[str, str]]


@pytest.fixture
def clone(tmp_path: Path, fixture_repo: FixtureRepo) -> Clone:
    dest = tmp_path / "ws" / "repo"
    sha = workspace.clone(RepoSource(fixture_repo.path.as_uri(), is_local=True), dest)
    entries, _ = workspace.tree_entries(Workspace(dest, sha))
    return Clone(dest, [asdict(entry) for entry in entries])


def test_blob_id_matches_git() -> None:
    assert sandbox.blob_id(b"hello\n", 40) == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_clean_workspace_is_copied_without_git(clone: Clone, tmp_path: Path) -> None:
    dest = tmp_path / "run"

    sandbox.snapshot(clone.repo, clone.entries, dest)

    assert (dest / "pkg" / "paginate.py").read_bytes() == (clone.repo / "pkg" / "paginate.py").read_bytes()
    assert (dest / "data.bin").read_bytes() == b"\x00\x01binary"
    assert not (dest / ".git").exists()
    copied = {path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()}
    assert copied == {entry["path"] for entry in clone.entries}


def edit_same_size(repo: Path) -> None:
    # Same length and the old mtime: git's stat cache would not notice, the hash does.
    target = repo / "pkg" / "paginate.py"
    stat = target.stat()
    content = target.read_bytes()
    changed = content.replace(b"line", b"LINE", 1)
    assert changed != content and len(changed) == len(content)
    target.write_bytes(changed)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda repo: (repo / "pkg" / "paginate.py").write_text("# changed by an agent\n"), "changed pkg/paginate.py"),
        (edit_same_size, "changed pkg/paginate.py"),
        (lambda repo: (repo / "conftest.py").write_text("import os\n"), "added conftest.py"),
        (
            lambda repo: (
                (repo / "pkg" / "__pycache__").mkdir() or (repo / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"0")
            ),
            "added pkg/__pycache__/x.pyc",
        ),
        (lambda repo: (repo / "README.md").unlink(), "missing README.md"),
    ],
    ids=["edit", "same-size-edit", "planted-conftest", "planted-pyc", "deleted"],
)
def test_dirty_workspace_is_refused(clone: Clone, tmp_path: Path, change: Any, expected: str) -> None:
    change(clone.repo)

    with pytest.raises(CliError) as exc:
        sandbox.snapshot(clone.repo, clone.entries, tmp_path / "run")
    assert exc.value.reason_code == "workspace_dirty"
    assert expected in exc.value.message


def test_edited_git_config_is_never_used(clone: Clone, tmp_path: Path) -> None:
    # A planted filter would run on the host if anything ran git here; the snapshot never does.
    marker = tmp_path / "ran"
    config = clone.repo / ".git" / "config"
    config.write_text(config.read_text() + f'[filter "x"]\n\tclean = touch "{marker.as_posix()}"\n')
    (clone.repo / ".gitattributes").write_text("* filter=x\n")

    with pytest.raises(CliError):
        sandbox.snapshot(clone.repo, clone.entries, tmp_path / "run")  # .gitattributes is an added file
    assert not marker.exists()


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", ".git/config", ""])
def test_unexpected_tree_paths_are_refused(clone: Clone, tmp_path: Path, path: str) -> None:
    with pytest.raises(CliError) as exc:
        sandbox.snapshot(clone.repo, [{"path": path, "mode": "100644", "blob": "0" * 40}], tmp_path / "run")
    assert exc.value.reason_code == "tree_invalid"


def test_missing_workspace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CliError) as exc:
        sandbox.snapshot(tmp_path / "nothing", [], tmp_path / "run")
    assert exc.value.reason_code == "workspace_missing"


def test_run_pinned_cleans_up_the_snapshot(clone: Clone, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def fake_run(_experiment_id: str, run_dir: Path, _command: str, _timeout: int) -> sandbox.SandboxResult:
        seen.append(run_dir)
        assert (run_dir / "README.md").is_file()
        return sandbox.SandboxResult("completed", 0, 1, "", "")

    monkeypatch.setattr(sandbox, "run", fake_run)

    sandbox.run_pinned(EXPERIMENT_ID, clone.repo, clone.entries, "true", 5)

    assert seen == [clone.repo.parent / "runs" / EXPERIMENT_ID]
    assert not seen[0].exists()


# --- real containers ------------------------------------------------------------------------------


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603, S607
        ["docker", "image", "inspect", sandbox.SANDBOX_IMAGE], capture_output=True, check=False
    )
    return probe.returncode == 0


needs_docker = pytest.mark.skipif(not docker_ready(), reason="needs Docker and the tracedesk-sandbox image")


@pytest.fixture
def seeded_repo() -> Iterator[Path]:
    """A tiny repo with one failing test. Created under the project's artifacts/ folder (gitignored),
    because Docker Desktop must be able to see the path (some temp folders are redirected per app)."""
    root = PROJECT_DIR / "artifacts" / "test-sandbox" / uuid.uuid4().hex
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "paginate.py").write_text("def page(items, n, size=2):\n    return items[n * size:(n + 1) * size]\n")
    (repo / "tests" / "test_paginate.py").write_text(
        "from paginate import page\n\n\ndef test_first_page():\n    assert page([1, 2, 3], 1) == [1, 2]\n"
    )
    yield repo
    shutil.rmtree(root, ignore_errors=True)


def run_in_sandbox(repo: Path, command: str, timeout: int = 60) -> sandbox.SandboxResult:
    return sandbox.run(str(uuid.uuid4()), repo, command, timeout)


@pytest.mark.docker
@needs_docker
def test_failing_pytest_completes_with_exit_code_1(seeded_repo: Path) -> None:
    result = run_in_sandbox(seeded_repo, "python -m pytest -q")

    assert result.status == "completed", result.stderr
    assert result.exit_code == 1
    assert "1 failed" in result.stdout
    assert not (seeded_repo / ".pytest_cache").exists()
    assert not list(seeded_repo.rglob("__pycache__"))


@pytest.mark.docker
@needs_docker
def test_network_is_off(seeded_repo: Path) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"

    result = run_in_sandbox(seeded_repo, f'python -c "{code}"')

    assert result.exit_code != 0
    assert "OSError" in result.stderr or "unreachable" in result.stderr.lower()


@pytest.mark.docker
@needs_docker
@pytest.mark.parametrize("target", ["/work/planted.py", "/etc/planted", "/planted"])
def test_writes_outside_tmp_fail(seeded_repo: Path, target: str) -> None:
    result = run_in_sandbox(seeded_repo, f"echo x > {target}")

    assert result.exit_code != 0
    assert "Read-only file system" in result.stderr
    assert not (seeded_repo / "planted.py").exists()


@pytest.mark.docker
@needs_docker
def test_tmp_is_writable_and_user_is_not_root(seeded_repo: Path) -> None:
    result = run_in_sandbox(seeded_repo, "echo ok > /tmp/x && cat /tmp/x && id -u")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.split() == ["ok", "1000"]


@pytest.mark.docker
@needs_docker
def test_timeout_removes_the_container(seeded_repo: Path) -> None:
    experiment_id = str(uuid.uuid4())

    result = sandbox.run(experiment_id, seeded_repo, "sleep 120", 3)

    assert result.status == "timed_out"
    listed = subprocess.run(  # noqa: S603, S607
        ["docker", "ps", "-a", "-q", "--filter", f"name={sandbox.container_name(experiment_id)}"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert listed.stdout.strip() == ""
