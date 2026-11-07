"""Score agent investigations of the seeded cases against their known answers.

Running a case is manual: the human posts the case's problem.md in the Switch room and approves the
Validator's experiment (a program cannot post into a room on this setup; see docs/SWITCH_NOTES.md).
Each finished run is recorded in eval/runs.json as {"case", "investigation_id"}. This script reads
those investigations back from the ledger API, scores them, and writes eval/results.md.

Run (API up, any valid token):  TRACEDESK_TOKEN=... python eval/run_eval.py
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
RUNS_FILE = EVAL_DIR / "runs.json"
RESULTS_FILE = EVAL_DIR / "results.md"
DEFAULT_API_URL = "http://localhost:8000"
RULED_OUT = {"contradicted", "rejected"}
FINISHED = {"completed", "failed", "timed_out"}
Ledger = dict[str, Any]


@dataclass(frozen=True)
class CaseScore:
    case: str
    investigation_id: str
    root_cause_correct: bool
    false_verified: int
    decoy_rejected: bool | None  # None: no hypothesis about the decoy was proposed
    experiments_run: int
    human_approvals: int
    time_to_verdict_s: int | None
    introducing_commit_found: bool


# --- scoring (pure) -------------------------------------------------------------------------------


def cites(evidence: dict[str, Any], target: dict[str, Any]) -> bool:
    """A code citation of the target file whose lines overlap the target range."""
    if evidence["source_type"] != "code_ref" or evidence["file_path"] != target["file"]:
        return False
    low, high = target["line_range"]
    return evidence["line_start"] <= high and evidence["line_end"] >= low


def evidence_of(ledger: Ledger, hypothesis_id: str) -> list[dict[str, Any]]:
    return [e for e in ledger["evidence"] if e["hypothesis_id"] == hypothesis_id]


def matches(ledger: Ledger, hypothesis: dict[str, Any], target: dict[str, Any], kind: str | None = None) -> bool:
    return any(cites(e, target) for e in evidence_of(ledger, hypothesis["id"]) if kind is None or e["kind"] == kind)


def first_verified_at(ledger: Ledger) -> datetime | None:
    times = [parse_time(e["created_at"]) for e in ledger["events"] if e["to_status"] == "verified"]
    return min(times, default=None)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def score_case(case: str, expected: dict[str, Any], ledger: Ledger, introducing_sha: str | None) -> CaseScore:
    verified = [h for h in ledger["hypotheses"] if h["status"] == "verified"]
    correct = [h for h in verified if matches(ledger, h, expected, kind="supporting")]
    about_decoy = [h for h in ledger["hypotheses"] if matches(ledger, h, expected["decoy"], kind="supporting")]
    # A hypothesis citing both the decoy and the real cause is about the real cause.
    about_decoy = [h for h in about_decoy if h not in correct and not matches(ledger, h, expected, kind="supporting")]
    verdict_at = first_verified_at(ledger)
    created = parse_time(ledger["investigation"]["created_at"])
    return CaseScore(
        case=case,
        investigation_id=ledger["investigation"]["id"],
        root_cause_correct=bool(correct),
        false_verified=len(verified) - len(correct),
        decoy_rejected=all(h["status"] in RULED_OUT for h in about_decoy) if about_decoy else None,
        experiments_run=sum(1 for x in ledger["experiments"] if x["status"] in FINISHED),
        human_approvals=sum(1 for x in ledger["experiments"] if x["approved_by"] and x["status"] != "rejected"),
        time_to_verdict_s=int((verdict_at - created).total_seconds()) if verdict_at else None,
        introducing_commit_found=introducing_sha is not None
        and any(e["commit_sha"] == introducing_sha for e in ledger["evidence"] if e["source_type"] == "git_history"),
    )


# --- results.md -------------------------------------------------------------------------------------


def _yes_no(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min {secs} s"


def render_results(
    scores: list[CaseScore], tracedesk_sha: str, model: str, today: str, notes: dict[str, str] | None = None
) -> str:
    lines = [
        "# Eval results",
        "",
        f"Date: {today} · TraceDesk commit `{tracedesk_sha[:12]}` · agents: {model}",
        "",
        "Each case is a seeded bug with a known answer (`eval/cases/<case>/expected.json`). One run per case,",
        "driven through Switch by a human posting the problem and approving experiments. Scores are computed",
        "from the ledger by `eval/run_eval.py`.",
        "",
        "| Case | Root cause correct | False verified | Decoy rejected | Experiments | Approvals | "
        "Time to verdict | Introducing commit cited |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.case} | {_yes_no(s.root_cause_correct)} | {s.false_verified} | {_yes_no(s.decoy_rejected)} | "
            f"{s.experiments_run} | {s.human_approvals} | {_duration(s.time_to_verdict_s)} | "
            f"{_yes_no(s.introducing_commit_found)} |"
        )
    correct = sum(s.root_cause_correct for s in scores)
    run_notes = [f"- **{case}**: {note}" for case, note in (notes or {}).items()]
    if run_notes:
        lines += ["", "Notes on individual runs:", "", *run_notes]
    lines += [
        "",
        f"**Totals**: {correct}/{len(scores)} root causes verified correctly, "
        f"{sum(s.false_verified for s in scores)} false verifications, "
        f"{sum(s.decoy_rejected is True for s in scores)}/{len(scores)} decoys rejected, "
        f"{sum(s.experiments_run for s in scores)} experiments run, {sum(s.human_approvals for s in scores)} approvals.",
        "",
        f"Scope: {len(scores)} small seeded cases, written by the builder, one run each. This shows that the",
        "loop works end to end and that the guards held; it is not a measure of general debugging accuracy.",
        "",
        "How to read it: *false verified* counts hypotheses the server let reach `verified` that do not match",
        "the known answer; the guards are meant to keep it at 0. *Time to verdict* runs from creating the",
        "investigation to its first `verified` and includes the time a human took to approve (and, when it",
        "happened, waiting out model rate limits), so it is not agent speed alone.",
        "",
    ]
    return "\n".join(lines)


# --- ledger and git access ------------------------------------------------------------------------


def fetch_ledger(client: httpx.Client, investigation_id: str) -> Ledger:
    def get(path: str, **params: str) -> Any:
        resp = client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    hypotheses = get("/hypotheses", investigation_id=investigation_id)
    return {
        "investigation": get(f"/investigations/{investigation_id}"),
        "hypotheses": hypotheses,
        "evidence": get("/evidence", investigation_id=investigation_id),
        "experiments": get("/experiments", investigation_id=investigation_id),
        "events": [e for h in hypotheses for e in get(f"/hypotheses/{h['id']}/events")],
    }


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603, S607


def introducing_sha(case: str, subject: str) -> str | None:
    repo = CASES_DIR / case / "repo"
    if not (repo / ".git").is_dir():
        return None
    for line in git("log", "--format=%H%x00%s", cwd=repo).splitlines():
        sha, _, line_subject = line.partition("\x00")
        if line_subject == subject:
            return sha
    return None


def main() -> None:
    runs = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
    token = os.environ.get("TRACEDESK_TOKEN", "").strip()
    if not token:
        sys.exit("Set TRACEDESK_TOKEN (any role) so the ledger can be read")
    headers = {"Authorization": f"Bearer {token}", "X-Actor": "eval"}
    api = os.environ.get("TRACEDESK_API", "").strip() or DEFAULT_API_URL
    scores = []
    with httpx.Client(base_url=api, headers=headers, timeout=30) as client:
        for run in runs["runs"]:
            expected = json.loads((CASES_DIR / run["case"] / "expected.json").read_text(encoding="utf-8"))
            sha = introducing_sha(run["case"], expected["introducing_commit_subject"])
            scores.append(score_case(run["case"], expected, fetch_ledger(client, run["investigation_id"]), sha))
    tracedesk_sha = git("rev-parse", "HEAD", cwd=EVAL_DIR)
    today = datetime.now(UTC).date().isoformat()
    notes = {run["case"]: run["note"] for run in runs["runs"] if run.get("note")}
    RESULTS_FILE.write_text(render_results(scores, tracedesk_sha, runs["model"], today, notes), encoding="utf-8")
    print(f"wrote {RESULTS_FILE} ({len(scores)} cases)")


if __name__ == "__main__":
    main()
