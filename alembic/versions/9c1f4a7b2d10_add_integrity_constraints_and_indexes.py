"""add integrity constraints and indexes

Adds:
- unique dose_logs.reminder_event_id  (a repeated button tap cannot double-count a dose)
- unique medications (user_id, lower(name))  (the app already treats names case-insensitively)
- index reminder_events (status, trigger_time)  (the scheduler polls on these every 60s)

Existing duplicates are merged first so the migration cannot fail on live data.

Revision ID: 9c1f4a7b2d10
Revises: 84bd07384b74
"""
from typing import Sequence, Union

from alembic import op

revision: str = "9c1f4a7b2d10"
down_revision: Union[str, None] = "84bd07384b74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Re-point child rows from duplicate medications onto the oldest one, per (user, lower(name)).
_MERGE_CHILDREN = """
WITH ranked AS (
    SELECT id,
           first_value(id) OVER (
               PARTITION BY user_id, lower(name) ORDER BY created_at, id
           ) AS keep_id
    FROM medications
)
UPDATE {table} t
   SET medication_id = r.keep_id
  FROM ranked r
 WHERE t.medication_id = r.id
   AND r.id <> r.keep_id
"""

_DELETE_DUPLICATE_MEDICATIONS = """
DELETE FROM medications m
 USING (
    SELECT id,
           first_value(id) OVER (
               PARTITION BY user_id, lower(name) ORDER BY created_at, id
           ) AS keep_id
    FROM medications
 ) r
 WHERE m.id = r.id
   AND r.id <> r.keep_id
"""

_DELETE_DUPLICATE_DOSE_LOGS = """
DELETE FROM dose_logs
 WHERE id IN (
    SELECT id FROM (
        SELECT id,
               row_number() OVER (
                   PARTITION BY reminder_event_id ORDER BY logged_at, id
               ) AS rn
        FROM dose_logs
    ) t
    WHERE t.rn > 1
 )
"""


def upgrade() -> None:
    op.execute(_DELETE_DUPLICATE_DOSE_LOGS)
    for table in ("dosage_times", "reminder_events", "dose_logs", "cart_items"):
        op.execute(_MERGE_CHILDREN.format(table=table))
    op.execute(_DELETE_DUPLICATE_MEDICATIONS)

    op.create_unique_constraint(
        "uq_dose_logs_reminder_event_id", "dose_logs", ["reminder_event_id"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_medications_user_lower_name "
        "ON medications (user_id, lower(name))"
    )
    op.create_index(
        "ix_reminder_events_status_trigger_time",
        "reminder_events",
        ["status", "trigger_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_events_status_trigger_time", table_name="reminder_events")
    op.execute("DROP INDEX IF EXISTS uq_medications_user_lower_name")
    op.drop_constraint("uq_dose_logs_reminder_event_id", "dose_logs", type_="unique")
