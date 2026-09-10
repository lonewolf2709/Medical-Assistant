import logging
import uuid
from datetime import datetime, timedelta, timezone, time

import pytz
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DosageTime, Medication, ReminderEvent, User

SLOT_TIMES: dict[str, time] = {
    "morning": time(8, 0),
    "evening": time(18, 0),
    "night": time(21, 0),
}

DEFAULT_TZ = pytz.timezone(settings.user_timezone)

logger = logging.getLogger(__name__)


async def timezone_for(db: AsyncSession, user_id: uuid.UUID):
    """The user's own timezone, falling back to the configured default.

    Reminders belong to a person's clock, so slot times ("morning") must be
    resolved per user rather than against one process-wide timezone.
    """
    name = await db.scalar(select(User.timezone).where(User.id == user_id))
    if not name:
        return DEFAULT_TZ
    try:
        return pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        logger.warning("User %s has an unknown timezone %r — using the default", user_id, name)
        return DEFAULT_TZ


def _to_utc(local_dt: datetime, tz=DEFAULT_TZ) -> datetime:
    """Convert a naive local datetime in `tz` to UTC."""
    return tz.localize(local_dt).astimezone(pytz.utc)


async def regenerate_dose_events(db: AsyncSession, medication: Medication) -> None:
    """Delete pending dose events and precompute new ones for the next N days."""
    await db.execute(
        delete(ReminderEvent).where(
            ReminderEvent.medication_id == medication.id,
            ReminderEvent.type == "dose",
            ReminderEvent.status == "pending",
        )
    )

    now = datetime.now(timezone.utc)
    tz = await timezone_for(db, medication.user_id)

    # Explicitly load dosage_times — lazy loading not allowed in async
    result = await db.execute(
        select(DosageTime).where(DosageTime.medication_id == medication.id)
    )
    dosage_times = result.scalars().all()

    for day_offset in range(settings.reminder_precompute_days):
        base_date = (now + timedelta(days=day_offset)).date()
        for dt in dosage_times:
            slot_time = SLOT_TIMES.get(dt.slot) if dt.type == "slot" else dt.custom_time
            if slot_time is None:
                continue
            # Combine as local time, then convert to UTC
            local_dt = datetime.combine(base_date, slot_time)
            trigger = _to_utc(local_dt, tz)
            if medication.course_end and trigger >= medication.course_end:
                continue  # finite course — no doses beyond its end
            if trigger > now:
                db.add(ReminderEvent(
                    user_id=medication.user_id,
                    medication_id=medication.id,
                    trigger_time=trigger,
                    type="dose",
                    status="pending",
                ))

    await db.commit()


async def regenerate_refill_event(db: AsyncSession, medication: Medication) -> None:
    """Recalculate and upsert the refill reminder event."""
    await db.execute(
        delete(ReminderEvent).where(
            ReminderEvent.medication_id == medication.id,
            ReminderEvent.type == "refill",
            ReminderEvent.status == "pending",
        )
    )

    if medication.daily_dose <= 0:
        await db.commit()
        return

    if medication.status != "active":
        await db.commit()
        return

    days_remaining = medication.remaining_quantity / medication.daily_dose
    days_until_alert = days_remaining - settings.refill_alert_days_before
    now = datetime.now(timezone.utc)

    if days_until_alert > 0:
        trigger = now + timedelta(days=days_until_alert)
    else:
        # Already inside the alert window (or out of stock) — warn on the next
        # poll rather than not at all. But this path is recomputed daily, so
        # suppress it if we warned recently, or it becomes a daily nag.
        last_alert = await db.scalar(
            select(func.max(ReminderEvent.trigger_time)).where(
                ReminderEvent.medication_id == medication.id,
                ReminderEvent.type == "refill",
                ReminderEvent.status == "sent",
            )
        )
        if last_alert and (now - last_alert) < timedelta(
            days=settings.refill_realert_cooldown_days
        ):
            logger.info(
                "Holding refill re-alert for %s — last warned %s ago",
                medication.name, now - last_alert,
            )
            await db.commit()
            return
        trigger = now

    db.add(ReminderEvent(
        user_id=medication.user_id,
        medication_id=medication.id,
        trigger_time=trigger,
        type="refill",
        status="pending",
    ))

    await db.commit()


def _dose_slot_time(dosage_time: DosageTime) -> time | None:
    return SLOT_TIMES.get(dosage_time.slot) if dosage_time.type == "slot" else dosage_time.custom_time


async def top_up_dose_events(
    db: AsyncSession, days: int | None = None, now: datetime | None = None
) -> int:
    """Extend every active medication's dose events to cover the next `days`.

    Events are only precomputed when a medication is added or changed, so without
    a periodic top-up a user who stops texting the bot silently stops getting
    reminders once the initial window runs out. Returns the number of medications
    that gained events. Idempotent: existing trigger times are never duplicated.
    """
    days = days or settings.reminder_precompute_days
    now = now or datetime.now(timezone.utc)

    medications = (
        await db.execute(select(Medication).where(Medication.status == "active"))
    ).scalars().all()

    topped_up = 0
    tz_cache: dict = {}
    for medication in medications:
        if medication.course_end and medication.course_end <= now:
            continue  # course is over; complete_finished_courses retires it

        if medication.user_id not in tz_cache:
            tz_cache[medication.user_id] = await timezone_for(db, medication.user_id)
        tz = tz_cache[medication.user_id]
        dosage_times = (
            await db.execute(
                select(DosageTime).where(DosageTime.medication_id == medication.id)
            )
        ).scalars().all()
        if not dosage_times:
            continue

        existing = set(
            (
                await db.execute(
                    select(ReminderEvent.trigger_time).where(
                        ReminderEvent.medication_id == medication.id,
                        ReminderEvent.type == "dose",
                    )
                )
            ).scalars().all()
        )

        created = 0
        for day_offset in range(days):
            base_date = (now + timedelta(days=day_offset)).date()
            for dosage_time in dosage_times:
                slot_time = _dose_slot_time(dosage_time)
                if slot_time is None:
                    continue
                trigger = _to_utc(datetime.combine(base_date, slot_time), tz)
                if medication.course_end and trigger >= medication.course_end:
                    continue
                if trigger <= now or trigger in existing:
                    continue
                db.add(ReminderEvent(
                    user_id=medication.user_id,
                    medication_id=medication.id,
                    trigger_time=trigger,
                    type="dose",
                    status="pending",
                ))
                existing.add(trigger)
                created += 1

        if created:
            topped_up += 1
            logger.info(
                "Topped up %d dose events for medication %s", created, medication.id
            )

    await db.commit()
    return topped_up


async def complete_finished_courses(
    db: AsyncSession, now: datetime | None = None
) -> list[Medication]:
    """Retire medications whose finite course has ended.

    Marks them `completed` and drops any leftover pending reminders. Returns the
    medications that were just completed, so the caller can tell the user.
    Medications with no `course_end` are ongoing and never completed here.
    """
    now = now or datetime.now(timezone.utc)

    medications = (
        await db.execute(
            select(Medication)
            .where(
                Medication.status == "active",
                Medication.course_end.isnot(None),
                Medication.course_end <= now,
            )
            # The active→completed flip is what makes the announcement once-only,
            # so lock the rows: without this two workers could both read the row
            # as active and both announce before either commits.
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    if not medications:
        return []

    for medication in medications:
        medication.status = "completed"

    await db.execute(
        delete(ReminderEvent).where(
            ReminderEvent.medication_id.in_([m.id for m in medications]),
            ReminderEvent.status == "pending",
        )
    )
    await db.commit()

    logger.info("Completed %d finished course(s)", len(medications))
    return list(medications)


async def refresh_refill_events(db: AsyncSession) -> int:
    """Recompute every active medication's refill projection.

    The projection is otherwise only calculated when stock or the schedule
    changes, so it drifts from reality as doses are taken, skipped or missed.
    Returns the number of medications reprojected.
    """
    medications = (
        await db.execute(select(Medication).where(Medication.status == "active"))
    ).scalars().all()

    for medication in medications:
        await regenerate_refill_event(db, medication)

    return len(medications)


async def run_daily_maintenance(db: AsyncSession) -> dict[str, int]:
    """Daily upkeep: extend the dose window, then reproject refill alerts."""
    topped_up = await top_up_dose_events(db)
    refreshed = await refresh_refill_events(db)
    return {"dose_windows_topped_up": topped_up, "refill_projections_refreshed": refreshed}
