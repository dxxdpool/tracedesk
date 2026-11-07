"""End-to-end tests of the `st` CLI against the in-process API and the test database.

`st.main.get_client` is replaced with a client on Starlette's TestClient, so every command
goes through the real routes, auth, guards and Postgres.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from typer.testing import CliRunner

from app.main import app
from app.state_machine import Role
from st import main as st_main
from st.client import DEFAULT_API_URL, ApiClient, http_client_from_env
from st.sandbox import SandboxResult
from tests.conftest import FixtureRepo, bearer

RunSt = Callable[..., Any]


@pytest.fixture
def run_st(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[RunSt]:
    """Run `st` as a role. Depends on `client` for its test-database override."""
    role_holder: dict[str, Role] = {"role": Role.AGENT}

    def fake_client() -> ApiClient:
        headers = {"X-Actor": "investigator", **bearer(role_holder["role"])}
        return ApiClient(TestClient(app, headers=headers))

    monkeypatch.setattr(st_main, "get_client", fake_client)
    runner = CliRunner()

    def run(*args: str, role: Role = Role.AGENT, input: str | None = None) -> Any:
        role_holder["role"] = role
        return runner.invoke(st_main.app, list(args), input=input)

    yield run


def created_id(result: Any) -> str:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["id"]


def test_full_flow_create_hypothesis_evidence_transition(run_st: RunSt) -> None:
    inv = created_id(
        run_st("investigation", "create", "--repo", "https://github.com/example/repo", "--problem", "bug", "--json")
    )
    hyp = created_id(run_st("hypothesis", "add", "--inv", inv, "--text", "off by one", "--json"))
    created_id(
        run_st(
            "evidence", "add", "--inv", inv, "--hypothesis", hyp, "--kind", "supporting",
            "--file", "pkg/paginate.py", "--lines", "10-24", "--note", "start=1", "--json",
        )
    )  # fmt: skip
    assert run_st("hypothesis", "transition", hyp, "--to", "under_review", "--reason", "plausible").exit_code == 0
    result = run_st("hypothesis", "transition", hyp, "--to", "supported", "--reason", "line 12")
    assert result.exit_code == 0, result.output
    assert "[supported]" in result.stdout

    shown = run_st("investigation", "show", inv)
    assert shown.exit_code == 0
    assert hyp in shown.stdout
    assert "[supported]" in shown.stdout
    assert "(by investigator)" in shown.stdout

    events = run_st("hypothesis", "events", hyp)
    assert "under_review -> supported  by investigator: line 12" in events.stdout


def test_rejection_prints_reason_and_exits_non_zero(run_st: RunSt) -> None:
    inv = created_id(run_st("investigation", "create", "--repo", "https://github.com/x/y", "--problem", "p", "--json"))
    hyp = created_id(run_st("hypothesis", "add", "--inv", inv, "--text", "t", "--json"))

    result = run_st("hypothesis", "transition", hyp, "--to", "verified", "--reason", "very confident")
    assert result.exit_code == 1
    assert "rejected (409 illegal_transition)" in result.stderr


def test_rejection_as_json(run_st: RunSt) -> None:
    inv = created_id(run_st("investigation", "create", "--repo", "https://github.com/x/y", "--problem", "p", "--json"))
    hyp = created_id(run_st("hypothesis", "add", "--inv", inv, "--text", "t", "--json"))
    run_st("hypothesis", "transition", hyp, "--to", "under_review", "--reason", "r")

    result = run_st("--json", "hypothesis", "transition", hyp, "--to", "supported", "--reason", "r")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["reason_code"] == "no_supporting_evidence"


def test_validation_error_is_readable(run_st: RunSt) -> None:
    result = run_st("hypothesis", "add", "--inv", "not-a-uuid", "--text", "t")
    assert result.exit_code == 1
    assert "invalid_request" in result.stderr
    assert "investigation_id" in result.stderr


def test_bad_lines_option_is_a_usage_error(run_st: RunSt) -> None:
    result = run_st("evidence", "add", "--inv", "x", "--kind", "supporting", "--file", "a.py", "--lines", "9-3")
    assert result.exit_code == 2


def test_unknown_status_rejected_locally(run_st: RunSt) -> None:
    result = run_st("hypothesis", "transition", "h", "--to", "maybe", "--reason", "r")
    assert result.exit_code == 2


def test_missing_env_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEDESK_TOKEN", raising=False)
    monkeypatch.setenv("TRACEDESK_ACTOR", "investigator")
    result = CliRunner().invoke(st_main.app, ["investigation", "show", "x"])
    assert result.exit_code == 1
    assert "TRACEDESK_TOKEN" in result.stderr


def test_unreachable_api_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEDESK_API", "http://127.0.0.1:9")
    monkeypatch.setenv("TRACEDESK_TOKEN", "t" * 20)
    monkeypatch.setenv("TRACEDESK_ACTOR", "investigator")
    result = CliRunner().invoke(st_main.app, ["investigation", "show", "x"])
    assert result.exit_code == 1
    assert "api_unreachable" in result.stderr


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("TRACEDESK_API", "TRACEDESK_TOKEN", "TRACEDESK_ACTOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_config_file_supplies_token_and_actor(clean_env: Path) -> None:
    (clean_env / "tracedesk.env").write_text(
        "# investigator\nTRACEDESK_TOKEN=file-token-123456\nTRACEDESK_ACTOR = investigator\n", encoding="utf-8"
    )
    with http_client_from_env() as http:
        assert http.headers["Authorization"] == "Bearer file-token-123456"
        assert http.headers["X-Actor"] == "investigator"


def test_config_file_cannot_change_api_url(clean_env: Path) -> None:
    # A cloned repo could plant this file; it must not redirect the token to another server.
    (clean_env / "tracedesk.env").write_text(
        "TRACEDESK_API=https://evil.example\nTRACEDESK_TOKEN=t\nTRACEDESK_ACTOR=a\n", encoding="utf-8"
    )
    with http_client_from_env() as http:
        assert str(http.base_url).startswith(DEFAULT_API_URL)


def test_environment_wins_over_config_file(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (clean_env / "tracedesk.env").write_text("TRACEDESK_TOKEN=from-file\nTRACEDESK_ACTOR=file\n", encoding="utf-8")
    monkeypatch.setenv("TRACEDESK_ACTOR", "from-env")
    with http_client_from_env() as http:
        assert http.headers["X-Actor"] == "from-env"
        assert http.headers["Authorization"] == "Bearer from-file"


@pytest.mark.parametrize(("value", "expected"), [("10-24", (10, 24)), ("7", (7, 7))])
def test_parse_lines(value: str, expected: tuple[int, int]) -> None:
    assert st_main.parse_lines(value) == expected


def test_investigation_create_prints_sha_and_workspace(run_st: RunSt) -> None:
    result = run_st("investigation", "create", "--repo", "https://github.com/example/repo", "--problem", "bug")

    assert result.exit_code == 0, result.output
    assert "sha:" in result.stdout
    assert "workspace:" in result.stdout
    assert "(not pinned)" not in result.stdout


def test_fabricated_citation_rejected_with_reason(run_st: RunSt) -> None:
    inv = created_id(run_st("investigation", "create", "--repo", "https://github.com/x/y", "--problem", "p", "--json"))

    result = run_st(
        "evidence", "add", "--inv", inv, "--kind", "supporting", "--file", "pkg/invented.py", "--lines", "1-3"
    )

    assert result.exit_code == 1
    assert "rejected (422 file_not_found)" in result.stderr


def test_real_citation_prints_server_quoted_snippet(run_st: RunSt) -> None:
    inv = created_id(run_st("investigation", "create", "--repo", "https://github.com/x/y", "--problem", "p", "--json"))

    result = run_st("evidence", "add", "--inv", inv, "--kind", "observation", "--file", "pkg/paginate.py", "--lines", "4")

    assert result.exit_code == 0, result.output
    assert "quoted by the server" in result.stdout
    assert "# line 4" in result.stdout


def test_commit_option_records_git_history(run_st: RunSt, fixture_repo: FixtureRepo) -> None:
    inv = created_id(run_st("investigation", "create", "--repo", "https://github.com/x/y", "--problem", "p", "--json"))

    result = run_st(
        "evidence", "add", "--inv", inv, "--kind", "observation", "--commit", fixture_repo.commits[0][:8], "--json"
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["source_type"] == "git_history"
    assert body["snippet"] == "add paginate helper"


# --- experiments -----------------------------------------------------------------------------


@dataclass
class FakeSandbox:
    """Stands in for Docker; records what would have run."""

    calls: list[tuple[str, Path, str, int]] = field(default_factory=list)

    status: str = "completed"

    def run(self, experiment_id: str, repo_dir: Path, command: str, timeout: int) -> SandboxResult:
        self.calls.append((experiment_id, repo_dir, command, timeout))
        assert (repo_dir / "pkg" / "paginate.py").is_file()  # the snapshot is in place while it runs
        assert not (repo_dir / ".git").exists()
        exit_code = 125 if self.status == "failed" else 1
        return SandboxResult(self.status, exit_code, 420, "tests/test_paginate.py F\n1 failed in 0.02s\n", "no image")


@pytest.fixture
def fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> FakeSandbox:
    fake = FakeSandbox()
    monkeypatch.setattr(st_main.sandbox, "run", fake.run)
    return fake


def requested_experiment(run_st: RunSt) -> tuple[str, str, str]:
    inv = created_id(run_st("investigation", "create", "--repo", "https://github.com/x/y", "--problem", "p", "--json"))
    hyp = created_id(run_st("hypothesis", "add", "--inv", inv, "--text", "off by one", "--json"))
    exp = created_id(
        run_st(
            "experiment", "request", "--hypothesis", hyp, "--command", "python -m pytest -q",
            "--why", "the test should fail on page 1", "--json",
        )
    )  # fmt: skip
    return inv, hyp, exp


def test_experiment_request_says_it_waits_for_a_human(run_st: RunSt) -> None:
    _inv, hyp, _exp = requested_experiment(run_st)

    result = run_st("experiment", "request", "--hypothesis", hyp, "--command", "pytest", "--why", "w")

    assert result.exit_code == 0, result.output
    assert "[requested]" in result.stdout
    assert "st approve" in result.stdout


def test_run_without_approval_is_refused_and_nothing_runs(run_st: RunSt, fake_sandbox: FakeSandbox) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)

    result = run_st("experiment", "run", exp)

    assert result.exit_code == 1
    assert "experiment_not_approved" in result.stderr
    assert fake_sandbox.calls == []


def test_agent_token_cannot_approve(run_st: RunSt) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)

    result = run_st("approve", exp, "--yes")

    assert result.exit_code == 1
    assert "rejected (403 role_not_allowed)" in result.stderr


def test_approve_shows_the_command_and_asks_first(run_st: RunSt) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)

    declined = run_st("approve", exp, role=Role.HUMAN, input="n\n")
    shown = run_st("experiment", "show", exp, "--json")

    assert declined.exit_code == 1
    assert "command: python -m pytest -q" in declined.stderr
    assert "not_approved" in declined.stderr
    assert json.loads(shown.stdout)["status"] == "requested"


def test_human_approves_after_confirming(run_st: RunSt) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)

    result = run_st("approve", exp, role=Role.HUMAN, input="y\n")

    assert result.exit_code == 0, result.output
    assert "approved by investigator" in result.stdout


def test_reject_as_human(run_st: RunSt, fake_sandbox: FakeSandbox) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)

    assert run_st("reject", exp, role=Role.HUMAN).exit_code == 0
    result = run_st("experiment", "run", exp)

    assert result.exit_code == 1
    assert "'rejected'" in result.stderr
    assert fake_sandbox.calls == []


def test_run_refuses_a_dirty_workspace(
    run_st: RunSt, fake_sandbox: FakeSandbox, workspace_settings: Path
) -> None:
    inv, _hyp, exp = requested_experiment(run_st)
    run_st("approve", exp, "--yes", role=Role.HUMAN)
    (workspace_settings / inv / "repo" / "conftest.py").write_text("raise SystemExit(0)\n")

    result = run_st("experiment", "run", exp)

    assert result.exit_code == 1
    assert "workspace_dirty" in result.stderr
    assert fake_sandbox.calls == []


def test_full_experiment_flow_ends_verified(run_st: RunSt, fake_sandbox: FakeSandbox, workspace_settings: Path) -> None:
    inv, hyp, exp = requested_experiment(run_st)
    run_st(
        "evidence", "add", "--inv", inv, "--hypothesis", hyp, "--kind", "supporting",
        "--file", "pkg/paginate.py", "--lines", "1-2",
    )  # fmt: skip
    run_st("hypothesis", "transition", hyp, "--to", "under_review", "--reason", "r")
    run_st("hypothesis", "transition", hyp, "--to", "supported", "--reason", "r")
    assert run_st("approve", exp, "--yes", role=Role.HUMAN).exit_code == 0

    ran = run_st("experiment", "run", exp, "--timeout", "30")
    by_agent = run_st("experiment", "conclude", exp, "--outcome", "confirms")
    concluded = run_st("experiment", "conclude", exp, "--outcome", "confirms", role=Role.VALIDATOR)
    verified = run_st(
        "hypothesis", "transition", hyp, "--to", "verified", "--reason", "test fails", role=Role.VALIDATOR
    )

    assert ran.exit_code == 0, ran.output
    assert "[completed] exit code 1" in ran.stdout
    assert "1 failed in 0.02s" in ran.stdout
    assert f"artifacts/{inv}/{exp}/stdout.log" in ran.stdout
    [(run_id, mounted, command, timeout)] = fake_sandbox.calls
    assert (run_id, command, timeout) == (exp, "python -m pytest -q", 30)
    # The sandbox gets a verified copy of the pinned files, removed after the run.
    assert mounted == workspace_settings / inv / "runs" / exp
    assert not mounted.exists()
    assert "rejected (403 role_not_allowed)" in by_agent.stderr
    assert concluded.exit_code == 0, concluded.output
    assert verified.exit_code == 0, verified.output
    assert "[verified]" in verified.stdout


def test_run_twice_is_refused(run_st: RunSt, fake_sandbox: FakeSandbox) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)
    run_st("approve", exp, "--yes", role=Role.HUMAN)
    run_st("experiment", "run", exp)

    again = run_st("experiment", "run", exp)

    assert again.exit_code == 1
    assert "'completed'" in again.stderr
    assert len(fake_sandbox.calls) == 1


def test_docker_failure_is_not_uploaded(run_st: RunSt, fake_sandbox: FakeSandbox) -> None:
    _inv, _hyp, exp = requested_experiment(run_st)
    run_st("approve", exp, "--yes", role=Role.HUMAN)
    fake_sandbox.status = "failed"

    result = run_st("experiment", "run", exp)
    shown = run_st("experiment", "show", exp, "--json")

    assert result.exit_code == 1
    assert "sandbox_failed" in result.stderr
    assert json.loads(shown.stdout)["status"] == "approved"  # the approval is not used up


def test_evidence_list_shows_citations_per_hypothesis(run_st: RunSt) -> None:
    inv, hyp, _exp = requested_experiment(run_st)
    other = created_id(run_st("hypothesis", "add", "--inv", inv, "--text", "decoy", "--json"))
    run_st("evidence", "add", "--inv", inv, "--hypothesis", hyp, "--kind", "supporting",
           "--file", "pkg/paginate.py", "--lines", "3-4", "--note", "start index")  # fmt: skip
    run_st("evidence", "add", "--inv", inv, "--hypothesis", other, "--kind", "contradicting",
           "--file", "README.md", "--lines", "1", "--note", "unrelated")  # fmt: skip

    everything = run_st("evidence", "list", "--inv", inv)
    only_hyp = run_st("evidence", "list", "--inv", inv, "--hypothesis", hyp)

    assert everything.exit_code == 0, everything.output
    assert "[supporting]  pkg/paginate.py:3-4  by investigator" in everything.stdout
    assert "[contradicting]  README.md:1-1" in everything.stdout
    assert "| # line 4" in everything.stdout
    assert "README.md" not in only_hyp.stdout
    assert "note: start index" in only_hyp.stdout


def test_run_timeout_is_bounded(run_st: RunSt) -> None:
    result = run_st("experiment", "run", "e", "--timeout", "9999")

    assert result.exit_code == 2
