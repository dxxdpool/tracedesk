"""Hypothesis state machine (CLAUDE.md section 6). Pure functions: no database, no I/O.

The caller gathers the facts (evidence counts, linked experiments, the actor's role from the
bearer token) into a TransitionContext; `can_transition` decides. Agent confidence or free text
is never an input, so it can never move a hypothesis.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.models import ExperimentOutcome, ExperimentStatus, HypothesisStatus

S = HypothesisStatus


class Role(StrEnum):
    """Who the bearer token belongs to. Never taken from the X-Actor header."""

    AGENT = "agent"
    VALIDATOR = "validator"
    HUMAN = "human"


ALLOWED_TRANSITIONS: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    S.PROPOSED: frozenset({S.UNDER_REVIEW, S.REJECTED, S.UNRESOLVED}),
    S.UNDER_REVIEW: frozenset({S.SUPPORTED, S.CONTRADICTED, S.REJECTED, S.UNRESOLVED}),
    S.SUPPORTED: frozenset({S.VERIFIED, S.CONTRADICTED, S.REJECTED, S.UNDER_REVIEW}),
    S.CONTRADICTED: frozenset({S.UNDER_REVIEW, S.REJECTED}),
    S.UNRESOLVED: frozenset({S.UNDER_REVIEW, S.REJECTED}),
    S.VERIFIED: frozenset(),
    S.REJECTED: frozenset(),
}

VERIFY_ROLES = frozenset({Role.VALIDATOR, Role.HUMAN})


@dataclass(frozen=True)
class ExperimentFacts:
    status: ExperimentStatus
    exit_code: int | None
    outcome: ExperimentOutcome | None
    approved_by: str | None

    @property
    def is_confirming(self) -> bool:
        # approved_by: results are only accepted after approval, but the guard checks it again itself.
        return (
            self.status == ExperimentStatus.COMPLETED
            and self.exit_code is not None
            and self.outcome == ExperimentOutcome.CONFIRMS
            and self.approved_by is not None
        )


@dataclass(frozen=True)
class TransitionContext:
    supporting_count: int
    contradicting_count: int
    experiments: tuple[ExperimentFacts, ...]
    actor_role: Role


@dataclass(frozen=True)
class Result:
    ok: bool
    reason_code: str | None = None
    message: str | None = None


ALLOWED = Result(ok=True)


def _reject(reason_code: str, message: str) -> Result:
    return Result(ok=False, reason_code=reason_code, message=message)


def can_transition(current: HypothesisStatus, target: HypothesisStatus, ctx: TransitionContext) -> Result:
    if not ALLOWED_TRANSITIONS[current]:
        return _reject("hypothesis_terminal", f"'{current}' is a final status; it cannot change")
    if target not in ALLOWED_TRANSITIONS[current]:
        allowed = ", ".join(sorted(ALLOWED_TRANSITIONS[current]))
        return _reject("illegal_transition", f"Cannot go from '{current}' to '{target}'. Allowed: {allowed}")
    return _check_guard(target, ctx)


def _check_guard(target: HypothesisStatus, ctx: TransitionContext) -> Result:
    if target == S.SUPPORTED and ctx.supporting_count < 1:
        return _reject("no_supporting_evidence", "Add at least one supporting evidence row first")
    if target == S.CONTRADICTED and ctx.contradicting_count < 1:
        return _reject("no_contradicting_evidence", "Add at least one contradicting evidence row first")
    if target == S.VERIFIED:
        if ctx.actor_role not in VERIFY_ROLES:
            return _reject("role_not_allowed", "Only the validator or a human can mark a hypothesis verified")
        if not any(experiment.is_confirming for experiment in ctx.experiments):
            return _reject(
                "no_confirming_experiment",
                "Verified needs a completed experiment on this hypothesis with an exit code and outcome 'confirms'",
            )
    return ALLOWED
