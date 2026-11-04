"""create ledger tables

Revision ID: 6dfd3cb3c35f
Revises:
Create Date: 2026-11-04 14:27:46.383900

Generated with --autogenerate, then edited by hand: enum columns are written as VARCHAR(32)
with explicit, named CHECK constraints. The allowed values are copied here on purpose, so a
later change to app/models.py cannot silently change what this migration did.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6dfd3cb3c35f"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_LENGTH = 32
INVESTIGATION_STATUSES = ("open", "concluded")
HYPOTHESIS_STATUSES = (
    "proposed", "under_review", "supported", "contradicted", "verified", "rejected", "unresolved",
)
EVIDENCE_KINDS = ("supporting", "contradicting", "observation")
EVIDENCE_SOURCES = ("code_ref", "git_history", "experiment_output", "human_note")
EXPERIMENT_STATUSES = (
    "requested", "approved", "rejected", "running", "completed", "failed", "timed_out",
)
EXPERIMENT_OUTCOMES = ("confirms", "refutes", "inconclusive")


def enum_column(name: str, *, nullable: bool = False, default: str | None = None) -> sa.Column:
    return sa.Column(name, sa.String(ENUM_LENGTH), nullable=nullable, server_default=default)


def check_in(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "investigations",
        uuid_pk(),
        sa.Column("repo_url", sa.String(length=2048), nullable=False),
        sa.Column("repo_sha", sa.String(length=40), nullable=True),
        sa.Column("problem", sa.Text(), nullable=False),
        enum_column("status", default="open"),
        sa.Column("switch_room_id", sa.String(length=255), nullable=True),
        created_at(),
        check_in("investigations", "status", INVESTIGATION_STATUSES),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
    )

    op.create_table(
        "hypotheses",
        uuid_pk(),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        enum_column("status", default="proposed"),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        check_in("hypotheses", "status", HYPOTHESIS_STATUSES),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"],
            name=op.f("fk_hypotheses_investigation_id_investigations"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hypotheses")),
    )
    op.create_index(op.f("ix_hypotheses_investigation_id"), "hypotheses", ["investigation_id"])

    op.create_table(
        "experiments",
        uuid_pk(),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        enum_column("status", default="requested"),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        enum_column("outcome", nullable=True),
        sa.Column("stdout_path", sa.String(length=1024), nullable=True),
        sa.Column("stderr_path", sa.String(length=1024), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        created_at(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        check_in("experiments", "status", EXPERIMENT_STATUSES),
        check_in("experiments", "outcome", EXPERIMENT_OUTCOMES),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.id"], name=op.f("fk_experiments_hypothesis_id_hypotheses")
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"],
            name=op.f("fk_experiments_investigation_id_investigations"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiments")),
    )
    op.create_index(op.f("ix_experiments_hypothesis_id"), "experiments", ["hypothesis_id"])
    op.create_index(op.f("ix_experiments_investigation_id"), "experiments", ["investigation_id"])

    op.create_table(
        "hypothesis_events",
        uuid_pk(),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=False),
        enum_column("from_status"),
        enum_column("to_status"),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        created_at(),
        check_in("hypothesis_events", "from_status", HYPOTHESIS_STATUSES),
        check_in("hypothesis_events", "to_status", HYPOTHESIS_STATUSES),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.id"],
            name=op.f("fk_hypothesis_events_hypothesis_id_hypotheses"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hypothesis_events")),
    )
    op.create_index(op.f("ix_hypothesis_events_hypothesis_id"), "hypothesis_events", ["hypothesis_id"])

    op.create_table(
        "evidence",
        uuid_pk(),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=True),
        sa.Column("experiment_id", sa.Uuid(), nullable=True),
        enum_column("kind"),
        enum_column("source_type"),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("repo_sha", sa.String(length=40), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        created_at(),
        check_in("evidence", "kind", EVIDENCE_KINDS),
        check_in("evidence", "source_type", EVIDENCE_SOURCES),
        sa.CheckConstraint(
            "(line_start IS NULL) = (line_end IS NULL)", name=op.f("ck_evidence_lines_both_or_neither")
        ),
        sa.CheckConstraint(
            "line_start IS NULL OR line_start >= 1", name=op.f("ck_evidence_line_start_positive")
        ),
        sa.CheckConstraint(
            "line_start IS NULL OR line_start <= line_end", name=op.f("ck_evidence_line_range_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["experiments.id"], name=op.f("fk_evidence_experiment_id_experiments")
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.id"], name=op.f("fk_evidence_hypothesis_id_hypotheses")
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"],
            name=op.f("fk_evidence_investigation_id_investigations"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index(op.f("ix_evidence_experiment_id"), "evidence", ["experiment_id"])
    op.create_index(op.f("ix_evidence_hypothesis_id"), "evidence", ["hypothesis_id"])
    op.create_index(op.f("ix_evidence_investigation_id"), "evidence", ["investigation_id"])


def downgrade() -> None:
    # Dropping a table drops its indexes and constraints too.
    for table in ("evidence", "hypothesis_events", "experiments", "hypotheses", "investigations"):
        op.drop_table(table)
