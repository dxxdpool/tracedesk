"""Cloned repositories, read only at the pinned commit.

Repo contents are untrusted. Nothing here runs repo code: clones have no hooks and no checkout
filters, symlinks are checked out as plain files, and every read goes through `git cat-file` at the
pinned SHA, so later changes to the working tree can never change what a citation quotes.
"""

import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CLONE_DEPTH = 200  # enough history for git log / blame on recent commits
CLONE_TIMEOUT_SECONDS = 120
MAX_REPO_BYTES = 200 * 1024 * 1024
SIZE_POLL_SECONDS = 1.0
GIT_TIMEOUT_SECONDS = 15
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RANGE_LINES = 200
MAX_LISTED_FILES = 5000
MAX_LOG_ENTRIES = 50
MAX_ERROR_CHARS = 500

GITHUB_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?"
)
COMMIT_ID = re.compile(r"[0-9a-fA-F]{4,40}")
LOG_FORMAT = "%H%x00%an%x00%aI%x00%s"
LOG_FIELDS = 4
LOCAL_PREFIX = "eval/cases/"
SYMLINK_MODE = "120000"

# Written into each clone's .git/config too, so git run later by agents on the host keeps them.
PERSISTED_GIT_CONFIG = {
    "credential.helper": "",  # no credential helper: no login popups for private or missing repos
    "core.hooksPath": os.devnull,
    "core.fsmonitor": "false",
    "core.symlinks": "false",  # symlinks become plain files, so nothing in the checkout points outside it
    "protocol.allow": "never",  # a clone never fetches again
}
# Applied to every git call as well (the clone itself re-allows exactly one protocol).
# safe.directory because the container and the host are different users; fsck refuses malformed objects.
SAFE_GIT_CONFIG: tuple[str, ...] = (
    *(arg for key, value in PERSISTED_GIT_CONFIG.items() for arg in ("-c", f"{key}={value}")),
    "-c", "safe.directory=*",
    "-c", "transfer.fsckObjects=true",
)  # fmt: skip
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_LITERAL_PATHSPECS": "1",  # a path like ":(icase)readme" must not match a different file
}
# git gets only these from the API's environment, never the tokens or DATABASE_URL.
ENV_PASSTHROUGH = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LANG")


class WorkspaceError(Exception):
    """A rejected repo, path or citation, with a machine-readable reason_code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class RepoSource:
    clone_url: str
    is_local: bool


@dataclass(frozen=True)
class Workspace:
    repo_dir: Path
    sha: str


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str
    date: str
    subject: str


@dataclass(frozen=True)
class BlameLine:
    line: int
    commit: str
    author: str
    summary: str
    text: str


Cloner = Callable[[RepoSource, Path], str]


# --- repo URLs ---------------------------------------------------------------------------------


def parse_repo_url(raw: str, *, allow_local: bool, local_dir: Path) -> RepoSource:
    """Accept `https://github.com/<owner>/<repo>` only, or (eval/dev) a repo under eval/cases/."""
    value = raw.strip()
    match = GITHUB_URL.fullmatch(value)
    if match and match["repo"] not in {".", ".."}:
        return RepoSource(f"https://github.com/{match['owner']}/{match['repo']}.git", is_local=False)
    if value.startswith(("http://", "https://", "git@", "ssh://", "git://", "file://")):
        raise WorkspaceError("repo_url_invalid", "Only https://github.com/<owner>/<repo> URLs are accepted")
    if not allow_local:
        raise WorkspaceError("repo_url_invalid", "Only https://github.com/<owner>/<repo> URLs are accepted")
    return RepoSource(_local_repo(value, local_dir).as_uri(), is_local=True)


def _local_repo(value: str, local_dir: Path) -> Path:
    root = local_dir.resolve()
    relative = value.replace("\\", "/")
    candidate = root / relative.removeprefix(LOCAL_PREFIX) if not Path(value).is_absolute() else Path(value)
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise WorkspaceError("local_repo_invalid", f"Local repos must be inside {LOCAL_PREFIX}")
    if not (resolved / ".git").is_dir():  # a .git *file* could point the clone anywhere
        raise WorkspaceError("local_repo_invalid", "The local path is not a git repository")
    return resolved


# --- cloning -----------------------------------------------------------------------------------


def workspace_repo_dir(workspaces_dir: Path, investigation_id: object) -> Path:
    return workspaces_dir / str(investigation_id) / "repo"


def clone(source: RepoSource, dest: Path) -> str:
    """Shallow-clone into `dest` (which must not exist) and return the exact HEAD SHA.

    Fetches without checking out, so a small but highly compressible repo cannot fill the disk:
    the files are written only after the sizes in the tree pass the cap. On failure `dest` is removed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    allowed = "file" if source.is_local else "https"
    command = [
        "git", *SAFE_GIT_CONFIG, "-c", f"protocol.{allowed}.allow=always",
        "clone", "--quiet", "--no-checkout", "--depth", str(CLONE_DEPTH), "--single-branch", "--no-tags",
        "--template=", "--", source.clone_url, str(dest),
    ]  # fmt: skip
    try:
        _run_clone(command, dest)
        for key, value in PERSISTED_GIT_CONFIG.items():
            _git(dest, "config", key, value)
        sha = _git(dest, "rev-parse", "HEAD").decode().strip()
        _check_checkout_size(dest, sha)
        _git(dest, "reset", "--quiet", "--hard", sha, timeout=CLONE_TIMEOUT_SECONDS)
        return sha
    except BaseException:
        remove_tree(dest)
        raise


def _too_large() -> WorkspaceError:
    return WorkspaceError("repo_too_large", f"Repository exceeds {MAX_REPO_BYTES // 2**20} MB")


def _check_checkout_size(repo_dir: Path, sha: str) -> None:
    listing = _git(repo_dir, "ls-tree", "-r", "-l", "-z", sha).decode(errors="replace")
    total = 0
    for entry in filter(None, listing.split("\x00")):
        size = entry.split("\t", 1)[0].split()[-1]  # "<mode> <type> <object> <size>\t<path>"
        total += int(size) if size.isdigit() else 0  # submodule entries have size "-"
    if total > MAX_REPO_BYTES:
        raise _too_large()


def _run_clone(command: list[str], dest: Path) -> None:
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(  # noqa: S603
            command, stdout=subprocess.DEVNULL, stderr=stderr, env=_git_env(), start_new_session=os.name == "posix"
        )
        deadline = time.monotonic() + CLONE_TIMEOUT_SECONDS
        while True:
            try:
                returncode = proc.wait(timeout=SIZE_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    _kill_tree(proc)
                    raise WorkspaceError("clone_timeout", f"Clone took longer than {CLONE_TIMEOUT_SECONDS}s") from None
                if tree_size(dest) > MAX_REPO_BYTES:
                    _kill_tree(proc)
                    raise _too_large() from None
        if returncode != 0:
            stderr.seek(0)
            detail = stderr.read().decode(errors="replace").strip()[:MAX_ERROR_CHARS]
            raise WorkspaceError("clone_failed", f"git clone failed: {detail or 'no details'}")
    if tree_size(dest) > MAX_REPO_BYTES:
        raise _too_large()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill git and its helpers (git-remote-https, index-pack), which would keep writing otherwise."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S603, S607
    proc.wait()


def tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue  # files come and go while git is writing
    return total


def remove_tree(path: Path) -> None:
    def make_writable(func: Callable[[str], object], target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)  # git marks pack files read-only; Windows refuses to delete them
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable)


# --- reading at the pinned commit --------------------------------------------------------------


def _git_env() -> dict[str, str]:
    passed = {name: os.environ[name] for name in ENV_PASSTHROUGH if name in os.environ}
    return {**passed, **GIT_ENV}


def _git(repo_dir: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> bytes:
    command = ["git", *SAFE_GIT_CONFIG, "-C", str(repo_dir), *args]
    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, timeout=timeout, env=_git_env(), check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError("git_timeout", f"git {args[0]} took longer than {timeout}s") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[:MAX_ERROR_CHARS]
        raise WorkspaceError("git_failed", f"git {args[0]} failed: {detail}")
    return result.stdout


def _check_workspace(ws: Workspace) -> None:
    if not (ws.repo_dir / ".git").is_dir():
        raise WorkspaceError("workspace_missing", "The cloned repository is missing from the workspace")


def normalize_repo_path(raw: str) -> str:
    """Repo-relative POSIX path. Rejects absolute paths, drive letters and any `..` component."""
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value) or "\x00" in value:
        raise WorkspaceError("path_invalid", "Give a path relative to the repository root")
    parts = [part for part in PurePosixPath(value).parts if part != "."]
    if not parts or ".." in parts:
        raise WorkspaceError("path_invalid", "The path must stay inside the repository")
    if parts[0] == ".git":
        raise WorkspaceError("path_invalid", "Paths inside .git cannot be cited")
    return "/".join(parts)


def safe_path(ws: Workspace, raw: str) -> Path:
    """Absolute path inside the checkout. Rejects traversal and symlinks that resolve outside it."""
    relative = normalize_repo_path(raw)
    root = ws.repo_dir.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceError("path_escapes_workspace", "The path resolves outside the repository")
    return resolved


def _blob_at_pin(ws: Workspace, relative: str) -> str:
    """Object id of a regular file at the pinned SHA, or a clear rejection."""
    entry = _git(ws.repo_dir, "ls-tree", "-z", ws.sha, "--", relative).decode(errors="replace").rstrip("\x00")
    if not entry:
        raise WorkspaceError("file_not_found", f"{relative} does not exist at commit {ws.sha[:12]}")
    meta, _, name = entry.partition("\t")
    if name != relative:  # belt and braces with GIT_LITERAL_PATHSPECS: quote exactly the cited file
        raise WorkspaceError("file_not_found", f"{relative} does not exist at commit {ws.sha[:12]}")
    mode, kind, object_id = meta.split(" ")
    if kind != "blob":
        raise WorkspaceError("not_a_file", f"{relative} is a {kind}, not a file")
    if mode == SYMLINK_MODE:
        raise WorkspaceError("file_is_symlink", f"{relative} is a symlink; cite its target instead")
    return object_id


def read_file(ws: Workspace, raw_path: str) -> tuple[str, list[str]]:
    """(normalized path, lines) of a text file exactly as it is at the pinned commit."""
    _check_workspace(ws)
    safe_path(ws, raw_path)
    relative = normalize_repo_path(raw_path)
    object_id = _blob_at_pin(ws, relative)
    size = int(_git(ws.repo_dir, "cat-file", "-s", object_id))
    if size > MAX_FILE_BYTES:
        raise WorkspaceError("file_too_large", f"{relative} is larger than {MAX_FILE_BYTES // 2**20} MB")
    content = _git(ws.repo_dir, "cat-file", "blob", object_id)
    if b"\x00" in content:
        raise WorkspaceError("file_not_text", f"{relative} is a binary file")
    text = content.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline does not start another line
    return relative, [line.removesuffix("\r") for line in lines]


def check_range(relative: str, line_count: int, start: int, end: int) -> None:
    if start < 1 or end < start:
        raise WorkspaceError("lines_invalid", "Lines start at 1 and start must be <= end")
    if end > line_count:
        raise WorkspaceError("lines_out_of_range", f"{relative} has {line_count} lines; asked for {start}-{end}")
    if end - start + 1 > MAX_RANGE_LINES:
        raise WorkspaceError("range_too_long", f"Cite at most {MAX_RANGE_LINES} lines at a time")


def read_lines(ws: Workspace, raw_path: str, start: int, end: int) -> tuple[str, list[str]]:
    """(normalized path, lines start..end) at the pinned commit; 1-based and inclusive."""
    relative, lines = read_file(ws, raw_path)
    check_range(relative, len(lines), start, end)
    return relative, lines[start - 1 : end]


def list_files(ws: Workspace) -> tuple[list[str], bool]:
    """(files at the pinned commit, truncated?)."""
    _check_workspace(ws)
    output = _git(ws.repo_dir, "ls-tree", "-r", "-z", "--name-only", ws.sha).decode(errors="replace")
    files = [name for name in output.split("\x00") if name]
    return files[:MAX_LISTED_FILES], len(files) > MAX_LISTED_FILES


def git_log(ws: Workspace, raw_path: str | None = None) -> list[CommitInfo]:
    """Recent commits up to the pinned SHA, optionally only those touching one path."""
    _check_workspace(ws)
    args = ["log", f"-n{MAX_LOG_ENTRIES}", ws.sha, "--"]
    if raw_path is not None:
        args.append(normalize_repo_path(raw_path))
    return _commits(ws.repo_dir, *args)


def _clean(text: str) -> str:
    """Commit text is repo-controlled: no control characters (terminal escapes) reach agents."""
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _commits(repo_dir: Path, *log_args: str) -> list[CommitInfo]:
    """Parse `git log` output with NUL separators, which commit text cannot contain."""
    output = _git(repo_dir, log_args[0], "-z", f"--format={LOG_FORMAT}", *log_args[1:])
    text = output.decode("utf-8", errors="replace").strip("\x00\n")
    if not text:
        return []
    fields = text.split("\x00")
    if len(fields) % LOG_FIELDS != 0:
        raise WorkspaceError("git_parse_failed", "Unexpected git log output")
    commits = []
    for i in range(0, len(fields), LOG_FIELDS):
        sha, author, date, subject = fields[i : i + LOG_FIELDS]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise WorkspaceError("git_parse_failed", "Unexpected git log output")
        commits.append(CommitInfo(sha, _clean(author), date, _clean(subject)))
    return commits


def git_blame(ws: Workspace, raw_path: str, start: int, end: int) -> list[BlameLine]:
    """Which commit last changed each line of a range, as of the pinned SHA."""
    relative, lines = read_file(ws, raw_path)
    check_range(relative, len(lines), start, end)
    output = _git(ws.repo_dir, "blame", "--line-porcelain", "-L", f"{start},{end}", ws.sha, "--", relative)
    return _parse_blame(output.decode("utf-8", errors="replace"))


def _parse_blame(output: str) -> list[BlameLine]:
    result: list[BlameLine] = []
    header: dict[str, str] = {}
    commit, line_no = "", 0
    for line in output.split("\n"):
        if line.startswith("\t"):
            text = line[1:].removesuffix("\r")
            result.append(BlameLine(line_no, commit, header.get("author", ""), header.get("summary", ""), text))
            header = {}
        elif not header and re.match(r"^[0-9a-f]{40} ", line):
            commit, _orig, final, *_ = line.split(" ")
            line_no = int(final)
            header = {"commit": commit}
        elif header:
            key, _, value = line.partition(" ")
            header[key] = value
    return result


def resolve_commit(ws: Workspace, raw: str) -> CommitInfo:
    """A commit that exists in the clone and is part of the pinned SHA's history."""
    _check_workspace(ws)
    value = raw.strip()
    if not COMMIT_ID.fullmatch(value):
        raise WorkspaceError("commit_invalid", "Give a commit SHA (4 to 40 hex characters)")
    try:
        full = _git(ws.repo_dir, "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}").decode().strip()
    except WorkspaceError as exc:
        raise WorkspaceError("commit_not_found", f"No commit {value} in the cloned history") from exc
    try:
        _git(ws.repo_dir, "merge-base", "--is-ancestor", full, ws.sha)
    except WorkspaceError as exc:
        raise WorkspaceError("commit_not_in_history", f"{full[:12]} is not an ancestor of the pinned commit") from exc
    return _commits(ws.repo_dir, "log", "-1", full)[0]
