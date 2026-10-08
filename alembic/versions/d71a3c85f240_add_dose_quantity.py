"""add per-dose quantity

A dose could only ever be one unit: daily_dose was the *number* of doses and
decrement always subtracted 1, so "two tablets twice a day" was unrepresentable
and every stock count and refill projection was wrong for it.

- dosage_times.dose_quantity: units taken at that time (default 1)
- reminder_events.dosage_time_id: which dose time produced the event, so the
  bot knows how many units it is worth
- medications.daily_dose becomes units per day, backfilled as
  (number of dose times) x 1, which equals its previous meaning

Revision ID: d71a3c85f240
Revises: c48f2b6e9a15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d71a3c85f240"
down_revision: Union[str, None] = "c48f2b6e9a15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dosage_times",
        sa.Column(
            "dose_quantity",
            sa.Float(),
            nullable=False,
            server_default="1",  # existing rows were implicitly one unit
        ),
    )
    op.add_column(
        "reminder_events",
        sa.Column("dosage_time_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_reminder_events_dosage_time_id",
        "reminder_events",
        "dosage_times",
        ["dosage_time_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Link existing pending dose events to their dose time where the local time
    # is unambiguous. Events left NULL fall back to one unit, the old behaviour.
    op.execute(
        """
        UPDATE reminder_events e
           SET dosage_time_id = d.id
          FROM dosage_times d
         WHERE e.medication_id = d.medication_id
           AND e.type = 'dose'
           AND e.status = 'pending'
           AND (
                SELECT count(*) FROM dosage_times d2
                 WHERE d2.medication_id = e.medication_id
               ) = 1
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_reminder_events_dosage_time_id", "reminder_events", type_="foreignkey"
    )
    op.drop_column("reminder_events", "dosage_time_id")
    op.drop_column("dosage_times", "dose_quantity")
