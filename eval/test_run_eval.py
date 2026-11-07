"""Scoring functions of eval/run_eval.py, on a hand-made ledger (no API, no database)."""

from typing import Any

import pytest

from run_eval import _duration, cites, render_results, score_case

EXPECTED = {
    "file": "shelf/paginate.py",
    "line_range": [11, 13],
    "decoy": {"file": "shelf/paginate.py", "line_range": [4, 8]},
}
INTRODUCING = "a" * 40


def code_ref(hypothesis: str, kind: str, file: str, start: int, end: int) -> dict[str, Any]:
    return {"hypothesis_id": hypothesis, "kind": kind, "source_type": "code_ref", "file_path": file,
            "line_start": start, "line_end": end, "commit_sha": None}  # fmt: skip


def ledger(**overrides: Any) -> dict[str, Any]:
    """A run like the real one: real cause verified, decoy contradicted, introducing commit cited."""
    base: dict[str, Any] = {
        "investigation": {"id": "inv-1", "created_at": "2026-11-06T10:30:00+00:00"},
        "hypotheses": [{"id": "real", "status": "verified"}, {"id": "decoy", "status": "contradicted"}],
        "evidence": [
            code_ref("real", "supporting", "shelf/paginate.py", 11, 13),
            code_ref("decoy", "supporting", "shelf/paginate.py", 4, 8),
            code_ref("decoy", "contradicting", "shelf/paginate.py", 4, 8),
            {
                "hypothesis_id": "real",
                "kind": "supporting",
                "source_type": "git_history",
                "file_path": "shelf/paginate.py",
                "line_start": None,
                "line_end": None,
                "commit_sha": INTRODUCING,
            },
        ],  # fmt: skip
        "experiments": [{"status": "completed", "approved_by": "human"}],
        "events": [{"to_status": "verified", "created_at": "2026-11-06T10:42:30+00:00"}],
    }
    return base | overrides


def test_perfect_run() -> None:
    score = score_case("pagination", EXPECTED, ledger(), INTRODUCING)

    assert score.root_cause_correct
    assert score.false_verified == 0
    assert score.decoy_rejected is True
    assert score.experiments_run == 1
    assert score.human_approvals == 1
    assert score.time_to_verdict_s == 750
    assert score.introducing_commit_found


def test_verifying_the_decoy_counts_as_false_verified() -> None:
    hypotheses = [{"id": "real", "status": "supported"}, {"id": "decoy", "status": "verified"}]

    score = score_case("pagination", EXPECTED, ledger(hypotheses=hypotheses), INTRODUCING)

    assert not score.root_cause_correct
    assert score.false_verified == 1
    assert score.decoy_rejected is False


def test_verified_without_a_matching_citation_is_not_correct() -> None:
    evidence = [code_ref("real", "supporting", "shelf/catalog.py", 1, 5)]

    score = score_case("pagination", EXPECTED, ledger(evidence=evidence), INTRODUCING)

    assert not score.root_cause_correct
    assert score.false_verified == 1


def test_contradicting_citation_of_the_cause_does_not_count() -> None:
    evidence = [code_ref("real", "contradicting", "shelf/paginate.py", 11, 13)]

    assert not score_case("pagination", EXPECTED, ledger(evidence=evidence), None).root_cause_correct


def test_nothing_verified() -> None:
    hypotheses = [{"id": "real", "status": "supported"}, {"id": "decoy", "status": "contradicted"}]

    score = score_case("pagination", EXPECTED, ledger(hypotheses=hypotheses, events=[]), INTRODUCING)

    assert not score.root_cause_correct
    assert score.false_verified == 0
    assert score.time_to_verdict_s is None


def test_decoy_never_proposed_is_not_applicable() -> None:
    evidence = [code_ref("real", "supporting", "shelf/paginate.py", 11, 13)]

    score = score_case(
        "pagination", EXPECTED, ledger(evidence=evidence, hypotheses=[{"id": "real", "status": "verified"}]), None
    )

    assert score.decoy_rejected is None
    assert not score.introducing_commit_found


def test_rejected_experiment_is_not_an_approval() -> None:
    experiments = [{"status": "rejected", "approved_by": "human"}, {"status": "approved", "approved_by": "human"}]

    score = score_case("pagination", EXPECTED, ledger(experiments=experiments), INTRODUCING)

    assert score.human_approvals == 1
    assert score.experiments_run == 0


@pytest.mark.parametrize(("seconds", "text"), [(None, "-"), (59, "0 min 59 s"), (78_480, "21 h 48 min")])
def test_duration_format(seconds: int | None, text: str) -> None:
    assert _duration(seconds) == text


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(11, 13, True), (13, 20, True), (1, 11, True), (14, 20, False), (1, 10, False)],
)
def test_citation_overlap(start: int, end: int, expected: bool) -> None:
    assert cites(code_ref("h", "supporting", "shelf/paginate.py", start, end), EXPECTED) is expected


def test_results_table_and_totals() -> None:
    score = score_case("pagination", EXPECTED, ledger(), INTRODUCING)

    markdown = render_results([score], "0123456789abcdef", "OpenCode test-model", "2026-11-07", {"pagination": "slow"})

    assert "| pagination | yes | 0 | yes | 1 | 1 | 12 min 30 s | yes |" in markdown
    assert "- **pagination**: slow" in markdown
    assert "**Totals**: 1/1 root causes verified correctly, 0 false verifications" in markdown
    assert "`0123456789ab`" in markdown
