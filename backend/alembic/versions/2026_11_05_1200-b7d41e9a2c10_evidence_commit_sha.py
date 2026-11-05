"""evidence.commit_sha for git_history evidence

Revision ID: b7d41e9a2c10
Revises: 6dfd3cb3c35f
Create Date: 2026-11-05 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d41e9a2c10"
down_revision: Union[str, Sequence[str], None] = "6dfd3cb3c35f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SHA_LENGTH = 40


def upgrade() -> None:
    op.add_column("evidence", sa.Column("commit_sha", sa.String(length=SHA_LENGTH), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence", "commit_sha")
