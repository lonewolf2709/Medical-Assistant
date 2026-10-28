"""add reminder_events.nudge_count

An ignored dose reminder gets one follow-up; this records how many were sent
so it can never become a nag.

Revision ID: e5b2c7d9f031
Revises: d71a3c85f240
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b2c7d9f031"
down_revision: Union[str, None] = "d71a3c85f240"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reminder_events",
        sa.Column("nudge_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("reminder_events", "nudge_count")
