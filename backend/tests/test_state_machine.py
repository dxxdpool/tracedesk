"""Unit tests for the pure hypothesis state machine: every (from, to) pair and every guard."""

import itertools

import pytest

from app.models import ExperimentOutcome, ExperimentStatus, HypothesisStatus
from app.state_machine import (
    ALLOWED_TRANSITIONS,
    ExperimentFacts,
    Role,
    TransitionContext,
    can_transition,
)

S = HypothesisStatus

CONFIRMING = ExperimentFacts(
    status=ExperimentStatus.COMPLETED, exit_code=1, outcome=ExperimentOutcome.CONFIRMS, approved_by="human"
)

# A context that satisfies every guard, so only the (from, to) table decides.
FULL_CONTEXT = TransitionContext(
    supporting_count=1, contradicting_count=1, experiments=(CONFIRMING,), actor_role=Role.VALIDATOR
)
EMPTY_CONTEXT = TransitionContext(supporting_count=0, contradicting_count=0, experiments=(), actor_role=Role.AGENT)

# The exact table from CLAUDE.md section 6, written out again so a change to the code shows up here.
EXPECTED_ALLOWED = {
    (S.PROPOSED, S.UNDER_REVIEW),
    (S.PROPOSED, S.REJECTED),
    (S.PROPOSED, S.UNRESOLVED),
    (S.UNDER_REVIEW, S.SUPPORTED),
    (S.UNDER_REVIEW, S.CONTRADICTED),
    (S.UNDER_REVIEW, S.REJECTED),
    (S.UNDER_REVIEW, S.UNRESOLVED),
    (S.SUPPORTED, S.VERIFIED),
    (S.SUPPORTED, S.CONTRADICTED),
    (S.SUPPORTED, S.REJECTED),
    (S.SUPPORTED, S.UNDER_REVIEW),
    (S.CONTRADICTED, S.UNDER_REVIEW),
    (S.CONTRADICTED, S.REJECTED),
    (S.UNRESOLVED, S.UNDER_REVIEW),
    (S.UNRESOLVED, S.REJECTED),
}
ALL_PAIRS = list(itertools.product(S, S))
LEGAL_PAIRS = sorted(EXPECTED_ALLOWED)
ILLEGAL_PAIRS = [pair for pair in ALL_PAIRS if pair not in EXPECTED_ALLOWED]


def test_table_matches_spec() -> None:
    actual = {(current, target) for current, targets in ALLOWED_TRANSITIONS.items() for target in targets}
    assert actual == EXPECTED_ALLOWED


@pytest.mark.parametrize(("current", "target"), LEGAL_PAIRS)
def test_legal_transition_allowed_when_guards_met(current: S, target: S) -> None:
    result = can_transition(current, target, FULL_CONTEXT)
    assert result.ok, result
    assert result.reason_code is None


@pytest.mark.parametrize(("current", "target"), ILLEGAL_PAIRS)
def test_illegal_transition_rejected(current: S, target: S) -> None:
    result = can_transition(current, target, FULL_CONTEXT)
    assert not result.ok
    expected = "hypothesis_terminal" if current in (S.VERIFIED, S.REJECTED) else "illegal_transition"
    assert result.reason_code == expected
    assert result.message


@pytest.mark.parametrize(("current", "target"), ILLEGAL_PAIRS)
def test_illegal_transition_rejected_with_empty_context(current: S, target: S) -> None:
    assert not can_transition(current, target, EMPTY_CONTEXT).ok


@pytest.mark.parametrize("target", list(S))
@pytest.mark.parametrize("current", [S.VERIFIED, S.REJECTED])
def test_terminal_states_allow_nothing(current: S, target: S) -> None:
    assert ALLOWED_TRANSITIONS[current] == frozenset()
    assert can_transition(current, target, FULL_CONTEXT).reason_code == "hypothesis_terminal"


def test_supported_requires_supporting_evidence() -> None:
    ctx = TransitionContext(supporting_count=0, contradicting_count=3, experiments=(), actor_role=Role.HUMAN)
    result = can_transition(S.UNDER_REVIEW, S.SUPPORTED, ctx)
    assert not result.ok
    assert result.reason_code == "no_supporting_evidence"


def test_supported_with_one_supporting_evidence() -> None:
    ctx = TransitionContext(supporting_count=1, contradicting_count=0, experiments=(), actor_role=Role.AGENT)
    assert can_transition(S.UNDER_REVIEW, S.SUPPORTED, ctx).ok


@pytest.mark.parametrize("current", [S.UNDER_REVIEW, S.SUPPORTED])
def test_contradicted_requires_contradicting_evidence(current: S) -> None:
    ctx = TransitionContext(supporting_count=5, contradicting_count=0, experiments=(), actor_role=Role.HUMAN)
    result = can_transition(current, S.CONTRADICTED, ctx)
    assert not result.ok
    assert result.reason_code == "no_contradicting_evidence"


def test_verified_requires_confirming_experiment() -> None:
    ctx = TransitionContext(supporting_count=3, contradicting_count=0, experiments=(), actor_role=Role.VALIDATOR)
    result = can_transition(S.SUPPORTED, S.VERIFIED, ctx)
    assert not result.ok
    assert result.reason_code == "no_confirming_experiment"


def test_verified_rejected_for_agent_role_even_with_experiment() -> None:
    ctx = TransitionContext(supporting_count=1, contradicting_count=0, experiments=(CONFIRMING,), actor_role=Role.AGENT)
    result = can_transition(S.SUPPORTED, S.VERIFIED, ctx)
    assert not result.ok
    assert result.reason_code == "role_not_allowed"


@pytest.mark.parametrize("role", [Role.VALIDATOR, Role.HUMAN])
def test_verified_allowed_for_validator_and_human(role: Role) -> None:
    ctx = TransitionContext(supporting_count=0, contradicting_count=0, experiments=(CONFIRMING,), actor_role=role)
    assert can_transition(S.SUPPORTED, S.VERIFIED, ctx).ok


@pytest.mark.parametrize(
    "experiment",
    [
        pytest.param(
            ExperimentFacts(ExperimentStatus.COMPLETED, 0, ExperimentOutcome.INCONCLUSIVE, "human"), id="inconclusive"
        ),
        pytest.param(ExperimentFacts(ExperimentStatus.COMPLETED, 0, ExperimentOutcome.REFUTES, "human"), id="refutes"),
        pytest.param(ExperimentFacts(ExperimentStatus.COMPLETED, 0, None, "human"), id="no-outcome"),
        pytest.param(
            ExperimentFacts(ExperimentStatus.COMPLETED, None, ExperimentOutcome.CONFIRMS, "human"), id="no-exit-code"
        ),
        pytest.param(ExperimentFacts(ExperimentStatus.FAILED, 1, ExperimentOutcome.CONFIRMS, "human"), id="failed"),
        pytest.param(
            ExperimentFacts(ExperimentStatus.TIMED_OUT, 1, ExperimentOutcome.CONFIRMS, "human"), id="timed-out"
        ),
        pytest.param(ExperimentFacts(ExperimentStatus.APPROVED, None, None, "human"), id="not-run"),
        pytest.param(
            ExperimentFacts(ExperimentStatus.COMPLETED, 1, ExperimentOutcome.CONFIRMS, None), id="never-approved"
        ),
    ],
)
def test_verified_rejected_without_completed_confirming_experiment(experiment: ExperimentFacts) -> None:
    ctx = TransitionContext(supporting_count=1, contradicting_count=0, experiments=(experiment,), actor_role=Role.HUMAN)
    result = can_transition(S.SUPPORTED, S.VERIFIED, ctx)
    assert not result.ok
    assert result.reason_code == "no_confirming_experiment"


def test_one_confirming_experiment_among_others_is_enough() -> None:
    others = (ExperimentFacts(ExperimentStatus.COMPLETED, 0, ExperimentOutcome.REFUTES, "human"), CONFIRMING)
    ctx = TransitionContext(supporting_count=0, contradicting_count=0, experiments=others, actor_role=Role.VALIDATOR)
    assert can_transition(S.SUPPORTED, S.VERIFIED, ctx).ok


def test_exit_code_zero_counts_as_set() -> None:
    passing = ExperimentFacts(ExperimentStatus.COMPLETED, 0, ExperimentOutcome.CONFIRMS, "human")
    ctx = TransitionContext(supporting_count=0, contradicting_count=0, experiments=(passing,), actor_role=Role.HUMAN)
    assert can_transition(S.SUPPORTED, S.VERIFIED, ctx).ok


@pytest.mark.parametrize("current", [S.PROPOSED, S.UNDER_REVIEW, S.CONTRADICTED, S.UNRESOLVED])
def test_verified_only_reachable_from_supported(current: S) -> None:
    assert can_transition(current, S.VERIFIED, FULL_CONTEXT).reason_code == "illegal_transition"
