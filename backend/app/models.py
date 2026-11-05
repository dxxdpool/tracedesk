"""Evidence Ledger tables (CLAUDE.md section 5).

Enums are stored as VARCHAR with a CHECK constraint (not Postgres enum types), so adding a
value later is a constraint change instead of an ALTER TYPE. Primary keys are UUIDs generated
by Postgres. Deleting an investigation removes its whole ledger (ON DELETE CASCADE); the other
links use the default NO ACTION, so a row that still has dependants cannot be deleted on its own.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SHA_LENGTH = 40
ACTOR_LENGTH = 100
PATH_LENGTH = 1024
URL_LENGTH = 2048


class InvestigationStatus(StrEnum):
    OPEN = "open"
    CONCLUDED = "concluded"


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


class ExperimentStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ExperimentOutcome(StrEnum):
    CONFIRMS = "confirms"
    REFUTES = "refutes"
    INCONCLUSIVE = "inconclusive"


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def string_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """VARCHAR column holding the enum's values, guarded by a named CHECK constraint."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def investigation_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), index=True)


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = uuid_pk()
    repo_url: Mapped[str] = mapped_column(String(URL_LENGTH))
    repo_sha: Mapped[str | None] = mapped_column(String(SHA_LENGTH))
    problem: Mapped[str] = mapped_column(Text)
    status: Mapped[InvestigationStatus] = mapped_column(
        string_enum(InvestigationStatus, "status"), server_default=InvestigationStatus.OPEN.value
    )
    switch_room_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = created_at()


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[uuid.UUID] = uuid_pk()
    investigation_id: Mapped[uuid.UUID] = investigation_fk()
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[HypothesisStatus] = mapped_column(
        string_enum(HypothesisStatus, "status"), server_default=HypothesisStatus.PROPOSED.value
    )
    created_by: Mapped[str] = mapped_column(String(ACTOR_LENGTH))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HypothesisEvent(Base):
    """Append-only transition log. Rows are written by the transition endpoint (Part C)."""

    __tablename__ = "hypothesis_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hypotheses.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[HypothesisStatus] = mapped_column(string_enum(HypothesisStatus, "from_status"))
    to_status: Mapped[HypothesisStatus] = mapped_column(string_enum(HypothesisStatus, "to_status"))
    actor: Mapped[str] = mapped_column(String(ACTOR_LENGTH))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = uuid_pk()
    investigation_id: Mapped[uuid.UUID] = investigation_fk()
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    command: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text)
    status: Mapped[ExperimentStatus] = mapped_column(
        string_enum(ExperimentStatus, "status"), server_default=ExperimentStatus.REQUESTED.value
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[ExperimentOutcome | None] = mapped_column(string_enum(ExperimentOutcome, "outcome"))
    stdout_path: Mapped[str | None] = mapped_column(String(PATH_LENGTH))
    stderr_path: Mapped[str | None] = mapped_column(String(PATH_LENGTH))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(String(ACTOR_LENGTH))
    approved_by: Mapped[str | None] = mapped_column(String(ACTOR_LENGTH))
    created_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint("(line_start IS NULL) = (line_end IS NULL)", name="lines_both_or_neither"),
        CheckConstraint("line_start IS NULL OR line_start >= 1", name="line_start_positive"),
        CheckConstraint("line_start IS NULL OR line_start <= line_end", name="line_range_ordered"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    investigation_id: Mapped[uuid.UUID] = investigation_fk()
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("experiments.id"), index=True)
    kind: Mapped[EvidenceKind] = mapped_column(string_enum(EvidenceKind, "kind"))
    source_type: Mapped[EvidenceSource] = mapped_column(string_enum(EvidenceSource, "source_type"))
    file_path: Mapped[str | None] = mapped_column(String(PATH_LENGTH))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    snippet: Mapped[str | None] = mapped_column(Text)
    # git_history evidence: the full SHA of the cited commit (its subject line is the snippet).
    commit_sha: Mapped[str | None] = mapped_column(String(SHA_LENGTH))
    note: Mapped[str | None] = mapped_column(Text)
    repo_sha: Mapped[str | None] = mapped_column(String(SHA_LENGTH))
    created_by: Mapped[str] = mapped_column(String(ACTOR_LENGTH))
    created_at: Mapped[datetime] = created_at()
