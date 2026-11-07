"""The investigation report: only verified causes are findings, and untrusted text stays inert."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.services.report import NO_ROOT_CAUSE, code, code_block, text
from app.state_machine import Role
from tests.conftest import bearer

HUMAN = {"X-Actor": "human", **bearer(Role.HUMAN)}
VALIDATOR = {"X-Actor": "validator", **bearer(Role.VALIDATOR)}
COMMAND = "python -m pytest -q tests/test_paginate.py"


def section(markdown: str, title: str) -> str:
    """The body of one `## title` section, up to the next `## ` heading."""
    start = markdown.index(f"\n## {title}\n")
    end = markdown.find("\n## ", start + 1)
    return markdown[start : end if end != -1 else None]


async def add_hypothesis(client: AsyncClient, investigation: dict[str, Any], text_: str) -> str:
    resp = await client.post("/hypotheses", json={"investigation_id": investigation["id"], "text": text_})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def cite(client: AsyncClient, investigation: dict[str, Any], hyp: str, kind: str, lines: tuple[int, int]) -> None:
    resp = await client.post(
        "/evidence",
        json={
            "investigation_id": investigation["id"], "hypothesis_id": hyp, "kind": kind, "source_type": "code_ref",
            "file_path": "pkg/paginate.py", "line_start": lines[0], "line_end": lines[1], "note": f"{kind} note",
        },
    )  # fmt: skip
    assert resp.status_code == 201, resp.text


async def move(client: AsyncClient, hyp: str, to: str, headers: dict[str, str] | None = None) -> None:
    resp = await client.post(f"/hypotheses/{hyp}/transition", json={"to": to, "reason": f"to {to}"}, headers=headers)
    assert resp.status_code == 200, resp.text


async def supported(client: AsyncClient, investigation: dict[str, Any], text_: str) -> str:
    hyp = await add_hypothesis(client, investigation, text_)
    await cite(client, investigation, hyp, "supporting", (3, 4))
    await move(client, hyp, "under_review")
    await move(client, hyp, "supported")
    return hyp


async def confirm(client: AsyncClient, hyp: str) -> str:
    exp = (await client.post("/experiments", json={"hypothesis_id": hyp, "command": COMMAND, "why": "w"})).json()["id"]
    await client.post(f"/experiments/{exp}/approve", headers=HUMAN)
    run = {"status": "completed", "exit_code": 1, "duration_ms": 900, "stdout": "1 failed\n", "stderr": ""}
    await client.post(f"/experiments/{exp}/result", json=run)
    await client.post(f"/experiments/{exp}/conclude", json={"outcome": "confirms"}, headers=VALIDATOR)
    return exp


async def report_of(client: AsyncClient, investigation: dict[str, Any]) -> str:
    resp = await client.get(f"/investigations/{investigation['id']}/report")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    return resp.text


async def test_unverified_hypothesis_never_appears_under_root_cause(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    await supported(client, investigation, "SUPPORTED-BUT-UNTESTED start index is off by one")
    proposed = await add_hypothesis(client, investigation, "PROPOSED-ONLY the cache is stale")

    markdown = await report_of(client, investigation)

    root_cause = section(markdown, "Root cause")
    assert NO_ROOT_CAUSE in root_cause
    assert "SUPPORTED-BUT-UNTESTED" not in root_cause
    assert "PROPOSED-ONLY" not in root_cause
    uncertainty = section(markdown, "Remaining uncertainty")
    assert "These are hypotheses, not findings" in uncertainty
    assert "(supported, still open)" in uncertainty
    assert "SUPPORTED-BUT-UNTESTED" in uncertainty
    assert f"Hypothesis {proposed[:8]} (proposed, still open)" in uncertainty


async def test_confirmed_but_not_verified_is_still_not_a_root_cause(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    hyp = await supported(client, investigation, "CONFIRMED-NOT-VERIFIED offset bug")
    await confirm(client, hyp)  # experiment confirms, but nobody moved the hypothesis to verified

    root_cause = section(await report_of(client, investigation), "Root cause")

    assert NO_ROOT_CAUSE in root_cause
    assert "CONFIRMED-NOT-VERIFIED" not in root_cause


async def test_verified_root_cause_with_its_experiment_and_evidence(
    client: AsyncClient, investigation: dict[str, Any]
) -> None:
    real = await supported(client, investigation, "REAL page offset ignores 1-based pages")
    decoy = await add_hypothesis(client, investigation, "DECOY page count rounding")
    await cite(client, investigation, decoy, "contradicting", (8, 8))
    await move(client, decoy, "under_review")
    await move(client, decoy, "contradicted")
    exp = await confirm(client, real)
    await move(client, real, "verified", headers=VALIDATOR)

    markdown = await report_of(client, investigation)

    root_cause = section(markdown, "Root cause")
    assert "REAL page offset" in root_cause
    assert "DECOY" not in root_cause
    assert NO_ROOT_CAUSE not in root_cause
    assert f"experiment `{exp[:8]}`: `{COMMAND}` approved by human, exit code 1" in root_cause
    assert "`pkg/paginate.py:3-4`: supporting note" in root_cause
    assert investigation["repo_sha"] in markdown
    relevant = section(markdown, "Relevant code")
    assert "# line 3\n# line 4" in relevant  # the server-captured snippet, fenced
    hypotheses = section(markdown, "Hypotheses")
    assert f"### Root cause {real[:8]}: verified" in hypotheses
    assert f"### Hypothesis {decoy[:8]}: contradicted" in hypotheses
    assert "| supported | verified | validator | to verified |" in hypotheses
    experiments = section(markdown, "Experiments")
    assert (
        f"| {exp[:8]} | {real[:8]} | `{COMMAND}` | tester | human | completed | 1 | 900 ms | confirms |" in experiments
    )
    assert "(contradicted, ruled out by contradicting evidence): DECOY" in section(markdown, "Remaining uncertainty")
    next_steps = section(markdown, "Recommended next steps")
    assert "Fix the verified cause in `pkg/paginate.py:3-4`." in next_steps
    assert f"re-run `{COMMAND}`: it should pass" in next_steps


async def test_untrusted_text_cannot_inject_markdown(client: AsyncClient, investigation: dict[str, Any]) -> None:
    await add_hypothesis(
        client, investigation, "x\n# Root cause\n<script>alert(1)</script> [click](http://evil.example) | a |"
    )

    markdown = await report_of(client, investigation)

    assert "<script>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "[click](" not in markdown
    assert r"\[click\](http://evil.example)" in markdown  # escaped brackets: plain text, not a link
    assert markdown.count("\n## Root cause\n") == 1
    assert not any(line.startswith("# Root cause") for line in markdown.splitlines())


async def test_report_for_unknown_investigation_returns_404(client: AsyncClient) -> None:
    resp = await client.get(f"/investigations/{uuid.uuid4()}/report")

    assert resp.status_code == 404
    assert resp.json()["reason_code"] == "investigation_not_found"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a*b_c", r"a\*b\_c"),
        ("[x](y)", r"\[x\](y)"),
        ("<b>&", "&lt;b&gt;&amp;"),
        ("line1\nline2", "line1 line2"),
        ("pipe | cell", r"pipe \| cell"),
    ],
)
def test_text_escapes_markdown(value: str, expected: str) -> None:
    assert text(value) == expected


def test_code_cannot_be_closed_by_backticks() -> None:
    assert code("a`b") == "``a`b``"
    assert code("`x`") == "`` `x` ``"


def test_code_block_fence_is_longer_than_content() -> None:
    block = code_block("before\n```\nafter")
    assert block[0] == "````" and block[-1] == "````"


def test_cli_report_writes_markdown_file(
    client: AsyncClient, investigation: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from app.main import app
    from st import main as st_main
    from st.client import ApiClient

    monkeypatch.setattr(st_main, "get_client", lambda: ApiClient(TestClient(app, headers={"X-Actor": "h", **HUMAN})))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(st_main.app, ["report", investigation["id"]])
    bad = CliRunner().invoke(st_main.app, ["report", "../../etc/passwd"])

    assert result.exit_code == 0, result.output
    written = tmp_path / "reports" / f"{investigation['id']}.md"
    assert written.read_text(encoding="utf-8").startswith(f"# Investigation {investigation['id'][:8]}")
    assert bad.exit_code == 2
