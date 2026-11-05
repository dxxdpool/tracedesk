"""`st`: the only way agents (and the human) change the Evidence Ledger.

Every command prints a short human-readable result, or the raw API JSON with --json.
A server rejection prints its reason_code and message to stderr and exits 1, so an agent
sees exactly why (for example `no_confirming_experiment`).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import Annotated, Any

import typer

from st.client import ApiClient, CliError, http_client_from_env

app = typer.Typer(no_args_is_help=True, help="TraceDesk Evidence Ledger CLI")
investigation_app = typer.Typer(no_args_is_help=True, help="Create and show investigations")
hypothesis_app = typer.Typer(no_args_is_help=True, help="Add hypotheses and move them through the state machine")
evidence_app = typer.Typer(no_args_is_help=True, help="Attach evidence to an investigation or hypothesis")
app.add_typer(investigation_app, name="investigation")
app.add_typer(hypothesis_app, name="hypothesis")
app.add_typer(evidence_app, name="evidence")


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
    lines = [f"{e['created_at']}  {e['from_status']} -> {e['to_status']}  by {e['actor']}: {e['reason']}" for e in events]
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
    source: Annotated[
        EvidenceSource | None, typer.Option(help="Default: from --commit/--experiment/--file")
    ] = None,
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
