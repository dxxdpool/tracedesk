"""Turn an agent's citation into evidence the server has checked itself.

Agents send only a path and line range (or a commit SHA). The quoted text is read by the server
from the pinned commit, so an agent cannot fabricate a snippet or cite a file that does not exist.
"""

from dataclasses import dataclass

from app.services.workspace import Workspace, read_file, read_lines, resolve_commit


@dataclass(frozen=True)
class Citation:
    file_path: str | None
    snippet: str
    commit_sha: str | None = None


MAX_SNIPPET_LINE_CHARS = 500
MAX_SNIPPET_CHARS = 16_000
TRUNCATED = " …[truncated by TraceDesk]"


def _bounded(lines: list[str]) -> str:
    """Quote the lines, cutting very long ones (minified code) so one citation cannot be megabytes."""
    cut = [line if len(line) <= MAX_SNIPPET_LINE_CHARS else line[:MAX_SNIPPET_LINE_CHARS] + TRUNCATED for line in lines]
    snippet = "\n".join(cut)
    return snippet if len(snippet) <= MAX_SNIPPET_CHARS else snippet[:MAX_SNIPPET_CHARS] + TRUNCATED


def capture_code_ref(ws: Workspace, file_path: str, line_start: int, line_end: int) -> Citation:
    relative, lines = read_lines(ws, file_path, line_start, line_end)
    return Citation(file_path=relative, snippet=_bounded(lines))


def capture_commit(ws: Workspace, commit: str, file_path: str | None) -> Citation:
    """git_history evidence: a commit in the pinned history; the snippet is its subject line.

    An optional file must exist at the pinned commit (it names what the commit is evidence about).
    """
    info = resolve_commit(ws, commit)
    relative = read_file(ws, file_path)[0] if file_path is not None else None
    return Citation(file_path=relative, snippet=info.subject, commit_sha=info.sha)
