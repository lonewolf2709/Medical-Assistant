"""add medications.course_end for finite courses

A medication had no end date, so a 14-day antibiotic course kept generating
reminders forever once the daily top-up task started rolling the window.
NULL means an ongoing medication (e.g. a vitamin).

Revision ID: b3e7a91c4d22
Revises: 9c1f4a7b2d10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7a91c4d22"
down_revision: Union[str, None] = "9c1f4a7b2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "medications",
        sa.Column("course_end", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing rows keep course_end = NULL, i.e. ongoing — the previous behaviour.


def downgrade() -> None:
    op.drop_column("medications", "course_end")
