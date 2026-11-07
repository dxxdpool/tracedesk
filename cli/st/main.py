"""`st`: the only way agents (and the human) change the Evidence Ledger.

Every command prints a short human-readable result, or the raw API JSON with --json.
A server rejection prints its reason_code and message to stderr and exits 1, so an agent
sees exactly why (for example `no_confirming_experiment`).
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer

from st import sandbox
from st.client import ApiClient, CliError, http_client_from_env

app = typer.Typer(no_args_is_help=True, help="TraceDesk Evidence Ledger CLI")
investigation_app = typer.Typer(no_args_is_help=True, help="Create and show investigations")
hypothesis_app = typer.Typer(no_args_is_help=True, help="Add hypotheses and move them through the state machine")
evidence_app = typer.Typer(no_args_is_help=True, help="Attach evidence to an investigation or hypothesis")
experiment_app = typer.Typer(no_args_is_help=True, help="Request, run and conclude sandbox experiments")
app.add_typer(investigation_app, name="investigation")
app.add_typer(hypothesis_app, name="hypothesis")
app.add_typer(evidence_app, name="evidence")
app.add_typer(experiment_app, name="experiment")


def get_client() -> ApiClient:
    """Replaced in tests with a client bound to the in-process app."""
    return ApiClient(http_client_from_env())


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    VERIFIED = "verified"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class EvidenceKind(StrEnum):
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"
    OBSERVATION = "observation"


class EvidenceSource(StrEnum):
    CODE_REF = "code_ref"
    GIT_HISTORY = "git_history"
    EXPERIMENT_OUTPUT = "experiment_output"
    HUMAN_NOTE = "human_note"


class ExperimentOutcome(StrEnum):
    CONFIRMS = "confirms"
    REFUTES = "refutes"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class State:
    json_output: bool


JsonFlag = Annotated[bool, typer.Option("--json", help="Print the raw API response as JSON")]


@app.callback()
def main(ctx: typer.Context, json_output: JsonFlag = False) -> None:
    ctx.obj = State(json_output=json_output)


def _wants_json(ctx: typer.Context, flag: bool) -> bool:
    # --json works both before the command (`st --json hypothesis add ...`) and after it.
    return flag or (isinstance(ctx.obj, State) and ctx.obj.json_output)


def _emit(as_json: bool, data: Any, text: str) -> None:
    typer.echo(json.dumps(data, indent=2) if as_json else text)


def reports_errors(func: Callable[..., None]) -> Callable[..., None]:
    """Turn a CliError into a clear message on stderr and exit code 1."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            func(*args, **kwargs)
        except CliError as exc:
            ctx: typer.Context = kwargs["ctx"]
            if _wants_json(ctx, kwargs.get("json_output", False)):
                typer.echo(json.dumps({"error": exc.as_dict()}), err=True)
            else:
                status = f"{exc.status_code} " if exc.status_code else ""
                typer.echo(f"rejected ({status}{exc.reason_code}): {exc.message}", err=True)
            raise typer.Exit(code=1) from exc

    return wrapper


def parse_lines(value: str) -> tuple[int, int]:
    """'10-24' -> (10, 24); '10' -> (10, 10)."""
    start, _, end = value.partition("-")
    try:
        line_start, line_end = int(start), int(end or start)
    except ValueError as exc:
        raise typer.BadParameter("use START-END, for example 10-24") from exc
    if line_start < 1 or line_end < line_start:
        raise typer.BadParameter("lines start at 1 and START must be <= END")
    return line_start, line_end


# --- investigation ---------------------------------------------------------------------------


@investigation_app.command("create")
@reports_errors
def investigation_create(
    ctx: typer.Context,
    repo: Annotated[str, typer.Option(help="https://github.com/<owner>/<repo>")],
    problem: Annotated[str, typer.Option(help="The bug, in the reporter's words")],
    room: Annotated[str | None, typer.Option(help="Switch room id")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Clone the repo at its current HEAD and open an investigation pinned to that SHA."""
    body = {"repo_url": repo, "problem": problem, "switch_room_id": room}
    created = get_client().post("/investigations", body)
    text = "\n".join(
        [
            f"investigation {created['id']} created",
            f"  sha:       {created['repo_sha']}",
            f"  workspace: {created['workspace_path']}",
        ]
    )
    _emit(_wants_json(ctx, json_output), created, text)


@investigation_app.command("show")
@reports_errors
def investigation_show(
    ctx: typer.Context,
    investigation_id: Annotated[str, typer.Argument(metavar="ID")],
    json_output: JsonFlag = False,
) -> None:
    client = get_client()
    investigation = client.get(f"/investigations/{investigation_id}")
    hypotheses = client.get("/hypotheses", params={"investigation_id": investigation_id})
    lines = [
        f"investigation {investigation['id']}  [{investigation['status']}]",
        f"  repo:    {investigation['repo_url']}  sha: {investigation['repo_sha'] or '(not pinned)'}",
        f"  workspace: {investigation['workspace_path'] or '(none)'}",
        f"  problem: {investigation['problem']}",
        f"  hypotheses ({len(hypotheses)}):",
    ]
    lines += [f"    {h['id']}  [{h['status']}]  {h['text']}  (by {h['created_by']})" for h in hypotheses]
    data = {"investigation": investigation, "hypotheses": hypotheses}
    _emit(_wants_json(ctx, json_output), data, "\n".join(lines))


# --- hypothesis ------------------------------------------------------------------------------


@hypothesis_app.command("add")
@reports_errors
def hypothesis_add(
    ctx: typer.Context,
    inv: Annotated[str, typer.Option(help="Investigation id")],
    text: Annotated[str, typer.Option(help="The suspected root cause")],
    json_output: JsonFlag = False,
) -> None:
    created = get_client().post("/hypotheses", {"investigation_id": inv, "text": text})
    _emit(_wants_json(ctx, json_output), created, f"hypothesis {created['id']} added [{created['status']}]")


@hypothesis_app.command("transition")
@reports_errors
def hypothesis_transition(
    ctx: typer.Context,
    hypothesis_id: Annotated[str, typer.Argument(metavar="H")],
    to: Annotated[HypothesisStatus, typer.Option(help="Target status")],
    reason: Annotated[str, typer.Option(help="Why; recorded in the transition log")],
    json_output: JsonFlag = False,
) -> None:
    updated = get_client().post(f"/hypotheses/{hypothesis_id}/transition", {"to": to.value, "reason": reason})
    _emit(_wants_json(ctx, json_output), updated, f"hypothesis {updated['id']} is now [{updated['status']}]")


@hypothesis_app.command("events")
@reports_errors
def hypothesis_events(
    ctx: typer.Context,
    hypothesis_id: Annotated[str, typer.Argument(metavar="H")],
    json_output: JsonFlag = False,
) -> None:
    events = get_client().get(f"/hypotheses/{hypothesis_id}/events")
    lines = [
        f"{e['created_at']}  {e['from_status']} -> {e['to_status']}  by {e['actor']}: {e['reason']}" for e in events
    ]
    _emit(_wants_json(ctx, json_output), events, "\n".join(lines) or "no transitions yet")


# --- evidence --------------------------------------------------------------------------------


def _source_for(
    source: EvidenceSource | None, experiment: str | None, file: str | None, commit: str | None
) -> EvidenceSource:
    if source is not None:
        return source
    if commit is not None:
        return EvidenceSource.GIT_HISTORY
    if experiment is not None:
        return EvidenceSource.EXPERIMENT_OUTPUT
    if file is not None:
        return EvidenceSource.CODE_REF
    raise typer.BadParameter("pass --file, --commit, --experiment or --source", param_hint="--source")


@evidence_app.command("add")
@reports_errors
def evidence_add(
    ctx: typer.Context,
    inv: Annotated[str, typer.Option(help="Investigation id")],
    kind: Annotated[EvidenceKind, typer.Option(help="supporting, contradicting or observation")],
    hypothesis: Annotated[str | None, typer.Option(help="Hypothesis id")] = None,
    file: Annotated[str | None, typer.Option(help="File path relative to the repo root")] = None,
    lines: Annotated[str | None, typer.Option(help="Line range, e.g. 10-24")] = None,
    commit: Annotated[str | None, typer.Option(help="Commit SHA (git_history evidence)")] = None,
    note: Annotated[str | None, typer.Option(help="What this shows")] = None,
    experiment: Annotated[str | None, typer.Option(help="Experiment id this evidence comes from")] = None,
    source: Annotated[EvidenceSource | None, typer.Option(help="Default: from --commit/--experiment/--file")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Cite code (--file + --lines) or a commit (--commit). The server checks the citation against
    the pinned commit and quotes the code itself; a citation that does not exist is rejected."""
    body: dict[str, Any] = {
        "investigation_id": inv,
        "hypothesis_id": hypothesis,
        "experiment_id": experiment,
        "kind": kind.value,
        "source_type": _source_for(source, experiment, file, commit).value,
        "file_path": file,
        "commit_sha": commit,
        "note": note,
    }
    if lines is not None:
        body["line_start"], body["line_end"] = parse_lines(lines)
    created = get_client().post("/evidence", body)
    text = f"evidence {created['id']} added [{created['kind']}]"
    if created["snippet"] is not None:
        text += f"\n  quoted by the server from {created['repo_sha'][:12]}:\n{created['snippet']}"
    _emit(_wants_json(ctx, json_output), created, text)


SNIPPET_PREVIEW_LINES = 3


def _cited(evidence: dict[str, Any]) -> str:
    if evidence["line_start"] is not None:
        return f"{evidence['file_path']}:{evidence['line_start']}-{evidence['line_end']}"
    if evidence["commit_sha"] is not None:
        return f"commit {evidence['commit_sha'][:12]}" + (
            f" ({evidence['file_path']})" if evidence["file_path"] else ""
        )
    if evidence["experiment_id"] is not None:
        return f"experiment {evidence['experiment_id']}"
    return evidence["source_type"]


def _describe_evidence(evidence: dict[str, Any]) -> list[str]:
    lines = [
        f"{evidence['id']}  [{evidence['kind']}]  {_cited(evidence)}  by {evidence['created_by']}",
        f"    hypothesis: {evidence['hypothesis_id'] or '-'}",
    ]
    if evidence["note"]:
        lines.append(f"    note: {evidence['note']}")
    if evidence["snippet"]:
        preview = evidence["snippet"].splitlines()[-SNIPPET_PREVIEW_LINES:]
        lines += [f"    | {line}" for line in preview]
    return lines


@evidence_app.command("list")
@reports_errors
def evidence_list(
    ctx: typer.Context,
    inv: Annotated[str, typer.Option(help="Investigation id")],
    hypothesis: Annotated[str | None, typer.Option(help="Only evidence for this hypothesis")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Every evidence row of an investigation (or one hypothesis), with what it cites."""
    params = {"investigation_id": inv} | ({"hypothesis_id": hypothesis} if hypothesis else {})
    rows = get_client().get("/evidence", params=params)
    text = "\n".join(line for row in rows for line in _describe_evidence(row)) or "no evidence yet"
    _emit(_wants_json(ctx, json_output), rows, text)


# --- report ----------------------------------------------------------------------------------


@app.command("report")
@reports_errors
def report(
    ctx: typer.Context,
    investigation_id: Annotated[str, typer.Argument(metavar="ID", help="Investigation id")],
    out: Annotated[Path | None, typer.Option(help="Where to write it (default: reports/<id>.md)")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Write the investigation's Markdown report, built by the server from the ledger."""
    try:
        canonical = str(uuid.UUID(investigation_id))  # the id becomes a file name, so it must be a real UUID
    except ValueError as exc:
        raise typer.BadParameter("must be an investigation id (a UUID)", param_hint="ID") from exc
    markdown = get_client().get_text(f"/investigations/{canonical}/report")
    target = out or Path("reports") / f"{canonical}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    _emit(_wants_json(ctx, json_output), {"path": str(target)}, f"report written to {target}")


# --- experiments -----------------------------------------------------------------------------

OUTPUT_TAIL_LINES = 40
ExperimentArg = Annotated[str, typer.Argument(metavar="E", help="Experiment id")]


def _describe(experiment: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"experiment {experiment['id']}  [{experiment['status']}]",
            f"  hypothesis: {experiment['hypothesis_id']}",
            f"  requested by: {experiment['requested_by']}",
            f"  why:     {experiment['why']}",
            f"  command: {experiment['command']}",
        ]
    )


def _tail(text: str) -> str:
    return "\n".join(text.rstrip().splitlines()[-OUTPUT_TAIL_LINES:])


@experiment_app.command("request")
@reports_errors
def experiment_request(
    ctx: typer.Context,
    hypothesis: Annotated[str, typer.Option(help="Hypothesis id the experiment tests")],
    command: Annotated[str, typer.Option(help="Shell command run in the repo root inside the sandbox")],
    why: Annotated[str, typer.Option(help="What the result will show")],
    json_output: JsonFlag = False,
) -> None:
    """Ask to run a command. Nothing runs until a human approves it with `st approve`."""
    created = get_client().post("/experiments", {"hypothesis_id": hypothesis, "command": command, "why": why})
    text = f"{_describe(created)}\n  waiting for a human: st approve {created['id']}"
    _emit(_wants_json(ctx, json_output), created, text)


@experiment_app.command("show")
@reports_errors
def experiment_show(ctx: typer.Context, experiment_id: ExperimentArg, json_output: JsonFlag = False) -> None:
    """Show an experiment: command, status, result, approver and outcome."""
    experiment = get_client().get(f"/experiments/{experiment_id}")
    lines = [_describe(experiment)]
    if experiment["exit_code"] is not None or experiment["duration_ms"] is not None:
        lines.append(f"  exit code: {experiment['exit_code']}  duration: {experiment['duration_ms']} ms")
        lines.append(f"  logs: {experiment['stdout_path']}  {experiment['stderr_path']}")
    lines.append(f"  approved by: {experiment['approved_by'] or '-'}  outcome: {experiment['outcome'] or '-'}")
    _emit(_wants_json(ctx, json_output), experiment, "\n".join(lines))


@app.command("approve")
@reports_errors
def approve(
    ctx: typer.Context,
    experiment_id: ExperimentArg,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
    json_output: JsonFlag = False,
) -> None:
    """Human only (approver token): show the exact command, confirm, then approve it."""
    client = get_client()
    experiment = client.get(f"/experiments/{experiment_id}")
    if experiment["status"] != "requested":
        raise CliError("experiment_not_requested", f"Experiment is already '{experiment['status']}'")
    typer.echo(_describe(experiment), err=True)
    if not yes and not typer.confirm("Approve running exactly this command in the sandbox?", err=True):
        raise CliError("not_approved", "Nothing was approved")
    approved = client.post(f"/experiments/{experiment_id}/approve", {})
    _emit(_wants_json(ctx, json_output), approved, f"experiment {approved['id']} approved by {approved['approved_by']}")


@app.command("reject")
@reports_errors
def reject(ctx: typer.Context, experiment_id: ExperimentArg, json_output: JsonFlag = False) -> None:
    """Human only (approver token): refuse to run the experiment."""
    rejected = get_client().post(f"/experiments/{experiment_id}/reject", {})
    _emit(_wants_json(ctx, json_output), rejected, f"experiment {rejected['id']} rejected by {rejected['approved_by']}")


@experiment_app.command("run")
@reports_errors
def experiment_run(
    ctx: typer.Context,
    experiment_id: ExperimentArg,
    timeout: Annotated[
        int, typer.Option(min=1, max=sandbox.MAX_TIMEOUT_SECONDS, help="Wall-clock limit in seconds")
    ] = sandbox.DEFAULT_TIMEOUT_SECONDS,
    json_output: JsonFlag = False,
) -> None:
    """Run an approved experiment in the sandbox on this machine and upload the result."""
    client = get_client()
    experiment = client.get(f"/experiments/{experiment_id}")
    if experiment["status"] != "approved":
        raise CliError(
            "experiment_not_approved",
            f"Experiment is '{experiment['status']}'; it runs only after a human approves it "
            f"(st approve {experiment['id']})",
        )
    investigation = client.get(f"/investigations/{experiment['investigation_id']}")
    if not investigation["workspace_path"] or not investigation["repo_sha"]:
        raise CliError("investigation_not_pinned", "The investigation has no cloned repo at a pinned SHA")
    tree = client.get(f"/investigations/{investigation['id']}/tree")
    if tree["truncated"]:
        raise CliError("repo_too_many_files", "The pinned tree is too large to verify before a run")
    repo_dir = Path(investigation["workspace_path"])
    typer.echo(
        f"checking the workspace, then running in the sandbox (limit {timeout}s): {experiment['command']}", err=True
    )
    result = sandbox.run_pinned(experiment["id"], repo_dir, tree["entries"], experiment["command"], timeout)
    if result.status == sandbox.STATUS_FAILED:
        # Docker itself could not start it (no image, name already in use by a run in progress, ...).
        # Nothing is uploaded, so the approval is not used up by a broken setup.
        raise CliError("sandbox_failed", f"The sandbox did not start: {_tail(result.stderr) or 'no details'}")
    stored = client.post(f"/experiments/{experiment['id']}/result", result.as_payload())
    lines = [
        f"experiment {stored['id']} [{stored['status']}] exit code {stored['exit_code']} in {stored['duration_ms']} ms",
        f"  logs: {stored['stdout_path']}  {stored['stderr_path']}",
        f"--- stdout (last {OUTPUT_TAIL_LINES} lines) ---",
        _tail(result.stdout) or "(empty)",
    ]
    if result.stderr.strip():
        lines += [f"--- stderr (last {OUTPUT_TAIL_LINES} lines) ---", _tail(result.stderr)]
    lines.append(f"next: st experiment conclude {stored['id']} --outcome confirms|refutes|inconclusive")
    _emit(_wants_json(ctx, json_output), stored, "\n".join(lines))


@experiment_app.command("conclude")
@reports_errors
def experiment_conclude(
    ctx: typer.Context,
    experiment_id: ExperimentArg,
    outcome: Annotated[ExperimentOutcome, typer.Option(help="confirms, refutes or inconclusive")],
    json_output: JsonFlag = False,
) -> None:
    """Record what the result means for the hypothesis (once)."""
    concluded = get_client().post(f"/experiments/{experiment_id}/conclude", {"outcome": outcome.value})
    _emit(_wants_json(ctx, json_output), concluded, f"experiment {concluded['id']} concluded: {concluded['outcome']}")
