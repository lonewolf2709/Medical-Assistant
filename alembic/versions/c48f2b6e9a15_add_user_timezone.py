"""add users.timezone

Reminders were resolved against one process-wide USER_TIMEZONE, so every user
was reminded on the same clock. NULL keeps the previous behaviour by falling
back to the USER_TIMEZONE setting.

Revision ID: c48f2b6e9a15
Revises: b3e7a91c4d22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c48f2b6e9a15"
down_revision: Union[str, None] = "b3e7a91c4d22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "timezone")
