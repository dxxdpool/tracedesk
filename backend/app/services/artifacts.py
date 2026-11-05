"""Experiment logs on disk, and the short excerpt quoted in the ledger.

Rows store paths like `artifacts/<investigation_id>/<experiment_id>/stdout.log`, relative to the
project root, which reads the same on the host and in the API container.
"""

import uuid
from pathlib import Path

ARTIFACTS_PREFIX = "artifacts"
STDOUT_NAME = "stdout.log"
STDERR_NAME = "stderr.log"
EXCERPT_LINES = 30
EXCERPT_CHARS = 4000


def write_logs(
    artifacts_dir: Path, investigation_id: uuid.UUID, experiment_id: uuid.UUID, stdout: str, stderr: str
) -> tuple[str, str]:
    """Write both streams (overwriting a previous attempt) and return their stored paths."""
    relative = Path(str(investigation_id), str(experiment_id))
    folder = artifacts_dir / relative
    folder.mkdir(parents=True, exist_ok=True)
    stored: list[str] = []
    for name, content in ((STDOUT_NAME, stdout), (STDERR_NAME, stderr)):
        (folder / name).write_text(content, encoding="utf-8", errors="replace", newline="")
        stored.append(f"{ARTIFACTS_PREFIX}/{relative.as_posix()}/{name}")
    return stored[0], stored[1]


def log_excerpt(stdout: str, stderr: str) -> str:
    """The tail of the output (where pytest prints its summary), small enough to quote in a report."""
    text = stdout.strip() or stderr.strip()
    tail = "\n".join(text.splitlines()[-EXCERPT_LINES:])[-EXCERPT_CHARS:]
    return tail.replace("\x00", "") or "(no output)"
