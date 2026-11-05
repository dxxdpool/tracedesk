"""Run one approved experiment in a locked-down container on the host.

The API never touches Docker (its container has no Docker socket). `st experiment run` calls this
on the host, then uploads the result. Before a run, the workspace is hashed against the pinned tree
and copied to a fresh folder, which is what gets mounted. The repo is untrusted, so the container
gets no network, a read-only root filesystem and repo mount, no capabilities, a non-root user and
hard limits. Writes are possible only in /tmp, a small in-memory tmpfs. A wall-clock timeout
removes the container.
"""

import hashlib
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any

from st.client import CliError

SANDBOX_IMAGE = "tracedesk-sandbox"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 64 * 1024  # kept per stream; the rest is read and discarded
TRUNCATED_MARKER = "\n…[output truncated by TraceDesk at 64 KB]\n"
DOCKER_RUN_FAILED = 125  # docker itself could not create or start the container
DOCKER_CLI_TIMEOUT_SECONDS = 30
READ_CHUNK_BYTES = 8192
MAX_LISTED_CHANGES = 5
SHA256_HEX_LENGTH = 64

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class SandboxResult:
    status: str
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def container_name(experiment_id: str) -> str:
    return f"st-exp-{uuid.UUID(experiment_id)}"  # a real UUID, so the name cannot carry docker flags


def docker_command(experiment_id: str, repo_dir: Path, command: str) -> list[str]:
    """The full `docker run` argv. The approved command goes to `sh -c` inside the container only."""
    source = str(repo_dir.resolve())
    if "," in source or '"' in source:
        raise CliError("workspace_path_invalid", "The workspace path must not contain commas or quotes")
    return [
        "docker", "run", "--rm", "--name", container_name(experiment_id),
        "--pull", "never",  # never fetch an image named tracedesk-sandbox from a registry
        "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
        "--ulimit", "nofile=1024:1024",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m", "--user", "1000:1000",
        # Output reaches us through the attached pipes only; Docker keeps no copy on its own disk.
        "--log-driver", "none",
        "--init",  # a tiny init reaps processes the command leaves behind
        "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work",
        # The repo is read-only, so pytest must not try to write .pytest_cache.
        "-e", "PYTEST_ADDOPTS=-p no:cacheprovider",
        "--mount", f"type=bind,source={source},target=/work,readonly",
        "-w", "/work", SANDBOX_IMAGE, "sh", "-c", command,
    ]  # fmt: skip


def run(experiment_id: str, repo_dir: Path, command: str, timeout_seconds: int) -> SandboxResult:
    return execute(docker_command(experiment_id, repo_dir, command), container_name(experiment_id), timeout_seconds)


def execute(argv: list[str], name: str, timeout_seconds: int) -> SandboxResult:
    """Run argv with a wall-clock timeout, keeping at most MAX_OUTPUT_BYTES of each stream."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
    except FileNotFoundError as exc:
        raise CliError("docker_missing", "docker is not installed or not on PATH") from exc
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 (both are PIPE)
    out, err = _Capture(proc.stdout), _Capture(proc.stderr)
    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if proc.poll() is None:  # timeout, Ctrl+C or any error: the container must not keep running
            remove_container(name)
            proc.kill()
            proc.wait()
            remove_container(name)  # again: the daemon may have created it after the first attempt
    out.join()
    err.join()
    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        return SandboxResult(STATUS_TIMED_OUT, None, duration_ms, out.text(), err.text())
    status = STATUS_FAILED if proc.returncode == DOCKER_RUN_FAILED else STATUS_COMPLETED
    return SandboxResult(status, proc.returncode, duration_ms, out.text(), err.text())


def remove_container(name: str) -> None:
    """Best effort: stop and delete the container (it may not exist yet, or Docker may be absent)."""
    try:
        subprocess.run(  # noqa: S603, S607
            ["docker", "rm", "--force", name], capture_output=True, timeout=DOCKER_CLI_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


class _Capture(threading.Thread):
    """Drain a pipe so the process never blocks on a full buffer; keep only the first bytes."""

    def __init__(self, stream: IO[bytes]) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._kept = bytearray()
        self._truncated = False
        self.start()

    def run(self) -> None:
        while chunk := self._stream.read(READ_CHUNK_BYTES):
            room = MAX_OUTPUT_BYTES - len(self._kept)
            if room > 0:
                self._kept += chunk[:room]
            if len(chunk) > room:
                self._truncated = True
        self._stream.close()

    def text(self) -> str:
        decoded = self._kept.decode("utf-8", errors="replace")
        return decoded + TRUNCATED_MARKER if self._truncated else decoded


# --- what the sandbox sees: a verified copy of the pinned commit ----------------------------------


def blob_id(content: bytes, object_id_length: int) -> str:
    """git's object id for a file's bytes (sha1, or sha256 for repos in the newer object format)."""
    data = f"blob {len(content)}\0".encode() + content
    return (hashlib.sha256(data) if object_id_length == SHA256_HEX_LENGTH else hashlib.sha1(data)).hexdigest()  # noqa: S324


def snapshot(repo_dir: Path, entries: list[dict[str, str]], dest: Path) -> None:
    """Copy the workspace into `dest`, refusing unless it is exactly the pinned tree.

    Agents can write to the workspace with their own tools; an edited test or a planted conftest.py
    would make the experiment say something about code that is not the pinned commit. `entries` is
    the API's list of files and blob ids at the pinned SHA. Each file is read once, hashed and written
    from those same bytes, so the sandbox mounts exactly what was checked. No git runs on the host
    (agents could have edited .git/config), and .git is not copied.
    """
    if not (repo_dir / ".git").is_dir():
        raise CliError("workspace_missing", f"No cloned repository at {repo_dir}")
    expected = {_safe_relative(entry["path"]): entry["blob"] for entry in entries}
    on_disk, problems = _files_on_disk(repo_dir)
    problems += [f"added {path}" for path in sorted(on_disk - expected.keys())]
    problems += [f"missing {path}" for path in sorted(expected.keys() - on_disk)]
    _refuse_if(problems)
    for path, blob in expected.items():
        content = (repo_dir / path).read_bytes()
        if blob_id(content, len(blob)) != blob:
            problems.append(f"changed {path}")
            continue
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _refuse_if(problems)


def _safe_relative(path: str) -> str:
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts or parts[0] == ".git":
        raise CliError("tree_invalid", f"Refusing an unexpected path in the pinned tree: {path!r}")
    return path


def _files_on_disk(repo_dir: Path) -> tuple[set[str], list[str]]:
    """Every file under the workspace except .git, plus any symlink found (checkouts have none)."""
    files: set[str] = set()
    problems: list[str] = []
    for root, dirs, names in os.walk(repo_dir):
        here = Path(root)
        if here == repo_dir:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in [*dirs, *names]:
            if (here / name).is_symlink():
                problems.append(f"symlink {(here / name).relative_to(repo_dir).as_posix()}")
        dirs[:] = [name for name in dirs if not (here / name).is_symlink()]
        files.update((here / name).relative_to(repo_dir).as_posix() for name in names)
    return files, problems


def _refuse_if(problems: list[str]) -> None:
    if problems:
        listed = "; ".join(problems[:MAX_LISTED_CHANGES])
        more = f" and {len(problems) - MAX_LISTED_CHANGES} more" if len(problems) > MAX_LISTED_CHANGES else ""
        raise CliError("workspace_dirty", f"The workspace differs from the pinned commit: {listed}{more}")


def run_pinned(
    experiment_id: str, repo_dir: Path, entries: list[dict[str, str]], command: str, timeout_seconds: int
) -> SandboxResult:
    """Snapshot the verified pinned files next to the clone, run the experiment on them, clean up."""
    run_dir = repo_dir.parent / "runs" / str(uuid.UUID(experiment_id))
    shutil.rmtree(run_dir, ignore_errors=True)
    try:
        snapshot(repo_dir, entries, run_dir)
        return run(experiment_id, run_dir, command, timeout_seconds)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
