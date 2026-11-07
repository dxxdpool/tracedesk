"""Build every seeded eval case as a real git repository, reproducibly.

Each case folder holds `case.json` (an ordered list of commits), one full snapshot of the files per
commit under `history/`, `problem.md` (the bug as a user would report it) and `expected.json` (the
known answer). This script recreates `eval/cases/<name>/repo` from scratch: fixed author, dates and
messages, LF line endings, so every machine gets the same files and the same commit SHAs. The repos
are gitignored; only their sources are committed.

Run: python eval/make_cases.py           (all cases)
     python eval/make_cases.py <name>    (one case)
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"
AUTHOR = "TraceDesk Eval"
EMAIL = "eval@tracedesk.invalid"
FIRST_COMMIT_EPOCH = 1_761_955_200  # 2025-11-01T00:00:00Z
SECONDS_BETWEEN_COMMITS = 86_400


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    command = ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-C", str(repo), *args]
    result = subprocess.run(command, capture_output=True, text=True, check=True, env=env)  # noqa: S603
    return result.stdout.strip()


def _writable(func, path, _exc):  # type: ignore[no-untyped-def]
    os.chmod(path, stat.S_IWRITE)  # git marks objects read-only; Windows refuses to delete them
    func(path)


def clear_worktree(repo: Path) -> None:
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child, onexc=_writable)
        else:
            child.unlink()


def copy_snapshot(source: Path, repo: Path) -> None:
    """Copy text files with LF endings, whatever this checkout of TraceDesk uses."""
    for path in sorted(source.rglob("*")):
        if path.is_file():
            target = repo / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def build_case(case_dir: Path) -> list[str]:
    """Recreate case_dir/repo and return the commit SHAs, oldest first."""
    spec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    repo = case_dir / "repo"
    if repo.exists():
        shutil.rmtree(repo, onexc=_writable)
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    for index, commit in enumerate(spec["commits"]):
        clear_worktree(repo)
        copy_snapshot(case_dir / commit["snapshot"], repo)
        date = f"{FIRST_COMMIT_EPOCH + index * SECONDS_BETWEEN_COMMITS} +0000"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": AUTHOR, "GIT_AUTHOR_EMAIL": EMAIL, "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": AUTHOR, "GIT_COMMITTER_EMAIL": EMAIL, "GIT_COMMITTER_DATE": date,
        }  # fmt: skip
        git(repo, "add", "--all")
        git(repo, "commit", "--quiet", "--message", commit["message"], env=env)
    return git(repo, "rev-list", "--reverse", "HEAD").split()


def main(names: list[str]) -> None:
    case_dirs = [CASES_DIR / name for name in names] or sorted(p.parent for p in CASES_DIR.glob("*/case.json"))
    for case_dir in case_dirs:
        shas = build_case(case_dir)
        print(f"{case_dir.name}: {len(shas)} commits, HEAD {shas[-1][:12]}")


if __name__ == "__main__":
    main(sys.argv[1:])
