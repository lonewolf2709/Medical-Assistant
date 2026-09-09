import logging
import uuid
from datetime import datetime, timedelta, timezone, time

import pytz
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DosageTime, Medication, ReminderEvent

SLOT_TIMES: dict[str, time] = {
    "morning": time(8, 0),
    "evening": time(18, 0),
    "night": time(21, 0),
}

USER_TZ = pytz.timezone(settings.user_timezone)

logger = logging.getLogger(__name__)


def _to_utc(local_dt: datetime) -> datetime:
    """Convert a naive local datetime to UTC."""
    return USER_TZ.localize(local_dt).astimezone(pytz.utc)


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
            trigger = _to_utc(local_dt)
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
        return

    if medication.status != "active":
        await db.commit()
        return

    days_remaining = medication.remaining_quantity / medication.daily_dose
    days_until_alert = days_remaining - settings.refill_alert_days_before

    now = datetime.now(timezone.utc)
    # Already inside the alert window (or out of stock) — warn on the next poll
    # rather than not at all, which is the case that matters most.
    trigger = now + timedelta(days=days_until_alert) if days_until_alert > 0 else now
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
    for medication in medications:
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
                trigger = _to_utc(datetime.combine(base_date, slot_time))
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
