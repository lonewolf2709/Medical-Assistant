import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DosageTime, Medication

SLOT_TIMES = {"morning": time(8, 0), "evening": time(18, 0), "night": time(21, 0)}
TABLETS_PER_STRIP = 10


async def get_medication(db: AsyncSession, user_id: uuid.UUID, name: str) -> Medication | None:
    """Case-insensitive exact lookup.

    Deliberately not ilike(): a name like "Croc%" coming out of the parser would
    otherwise match a different medication.
    """
    result = await db.execute(
        select(Medication).where(
            Medication.user_id == user_id,
            func.lower(Medication.name) == (name or "").strip().lower(),
        )
    )
    return result.scalars().first()


async def add_or_update_medication(
    db: AsyncSession,
    user_id: uuid.UUID,
    name: str,
    dosage_times: list[str],
    total_quantity: float | None,
    duration_days: int | None = None,
    dose_quantity: float | None = None,
) -> Medication:
    """Create or update a medication.

    `duration_days` marks a finite course ("take it for 5 days"); without it the
    medication is ongoing and reminders continue until the user stops it.

    `dose_quantity` is how many units each dose is ("two tablets"), defaulting
    to one. `daily_dose` is therefore units per day, not doses per day.
    """
    per_dose = float(dose_quantity) if dose_quantity else 1.0
    units_per_day = len(dosage_times) * per_dose
    med = await get_medication(db, user_id, name)
    if med is None:
        qty = total_quantity or 0.0
        med = Medication(
            user_id=user_id,
            name=name,
            total_quantity=qty,
            remaining_quantity=qty,
            daily_dose=units_per_day,
        )
        db.add(med)
        await db.flush()
    else:
        if total_quantity is not None:
            med.total_quantity = total_quantity
            med.remaining_quantity = total_quantity
        med.daily_dose = units_per_day
        # Remove old dosage times via explicit delete query (no lazy load)
        from sqlalchemy import delete as sa_delete
        await db.execute(sa_delete(DosageTime).where(DosageTime.medication_id == med.id))
        await db.flush()

    # Explicitly adding a medication again restarts it — otherwise a finished
    # course could never be picked back up.
    if med.status in ("completed", "stopped"):
        med.status = "active"
        med.paused_until = None
        if duration_days is None:
            # Restarted without a new duration → treat it as ongoing, else the
            # stale course_end would block every event and re-complete it.
            med.course_end = None

    if duration_days is not None and duration_days > 0:
        med.course_end = datetime.now(timezone.utc) + timedelta(days=duration_days)

    _attach_dosage_times(db, med, dosage_times, per_dose)
    await db.commit()
    await db.refresh(med)
    return med


def _attach_dosage_times(
    db: AsyncSession, med: Medication, dosage_times: list[str], dose_quantity: float = 1.0
) -> None:
    for t in dosage_times:
        if t in SLOT_TIMES:
            db.add(DosageTime(
                medication_id=med.id, type="slot", slot=t, dose_quantity=dose_quantity
            ))
        else:
            h, m = map(int, t.split(":"))
            db.add(DosageTime(
                medication_id=med.id, type="custom", custom_time=time(h, m),
                dose_quantity=dose_quantity,
            ))


async def apply_refill(
    db: AsyncSession,
    user_id: uuid.UUID,
    name: str,
    quantity: float,
    unit: str,
) -> Medication | None:
    med = await get_medication(db, user_id, name)
    if med is None:
        return None
    tablets = quantity * TABLETS_PER_STRIP if unit == "strip" else quantity
    med.remaining_quantity += tablets
    med.total_quantity += tablets
    await db.commit()
    await db.refresh(med)
    return med


async def dose_units_for_event(db: AsyncSession, event) -> float:
    """How many units this particular reminder is worth."""
    if event.dosage_time_id:
        dosage_time = await db.get(DosageTime, event.dosage_time_id)
        if dosage_time:
            return dosage_time.dose_quantity
    return 1.0  # events created before dose quantities existed


async def decrement_dose(db: AsyncSession, event) -> None:
    """Reduce stock by the units this dose was actually worth."""
    med = await db.get(Medication, event.medication_id)
    if med is None:
        return
    units = await dose_units_for_event(db, event)
    med.remaining_quantity = max(0.0, med.remaining_quantity - units)
    await db.commit()


async def list_medications(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    """Return all medications for a user with dosage times and next reminder."""
    from datetime import datetime, timezone
    from app.models import ReminderEvent

    result = await db.execute(
        select(Medication).where(Medication.user_id == user_id)
    )
    medications = result.scalars().all()

    output = []
    for med in medications:
        # Get dosage times
        dt_result = await db.execute(
            select(DosageTime).where(DosageTime.medication_id == med.id)
        )
        dosage_times = dt_result.scalars().all()
        times = []
        for dt in dosage_times:
            if dt.type == "slot":
                times.append(dt.slot)
            else:
                times.append(dt.custom_time.strftime("%H:%M") if dt.custom_time else "?")

        # Get next pending reminder
        now = datetime.now(timezone.utc)
        next_event_result = await db.execute(
            select(ReminderEvent)
            .where(
                ReminderEvent.medication_id == med.id,
                ReminderEvent.type == "dose",
                ReminderEvent.status == "pending",
                ReminderEvent.trigger_time > now,
            )
            .order_by(ReminderEvent.trigger_time)
            .limit(1)
        )
        next_event = next_event_result.scalar_one_or_none()

        output.append({
            "name": med.name,
            "daily_dose": med.daily_dose,
            "remaining_quantity": med.remaining_quantity,
            "times": times,
            "next_reminder": next_event.trigger_time if next_event else None,
        })

    return output


async def stop_medication(db: AsyncSession, user_id: uuid.UUID, name: str) -> Medication | None:
    """Permanently stop a medication and cancel all pending reminders."""
    from sqlalchemy import delete as sa_delete
    from app.models import ReminderEvent
    med = await get_medication(db, user_id, name)
    if med is None:
        return None
    med.status = "stopped"
    await db.execute(
        sa_delete(ReminderEvent).where(
            ReminderEvent.medication_id == med.id,
            ReminderEvent.status == "pending",
        )
    )
    await db.commit()
    return med


async def pause_medication(db: AsyncSession, user_id: uuid.UUID, name: str, days: int | None) -> Medication | None:
    """Pause a medication for N days (or indefinitely) and cancel pending reminders."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import delete as sa_delete
    from app.models import ReminderEvent
    med = await get_medication(db, user_id, name)
    if med is None:
        return None
    med.status = "paused"
    med.paused_until = datetime.now(timezone.utc) + timedelta(days=days) if days else None
    await db.execute(
        sa_delete(ReminderEvent).where(
            ReminderEvent.medication_id == med.id,
            ReminderEvent.status == "pending",
        )
    )
    await db.commit()
    return med


async def resume_medication(db: AsyncSession, user_id: uuid.UUID, name: str) -> Medication | None:
    """Resume a paused medication and regenerate reminders."""
    med = await get_medication(db, user_id, name)
    if med is None:
        return None
    if med.status not in ("paused", "stopped"):
        return med  # already active
    med.status = "active"
    med.paused_until = None
    await db.commit()
    await db.refresh(med)
    return med


async def get_adherence_stats(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Adherence % per medication.

    The denominator is the number of dose reminders actually sent — a reminder the
    user ignored counts as missed. Counting only answered reminders would report
    100% for someone who tapped "Taken" once and ignored everything after.
    """
    from app.models import DoseLog, ReminderEvent

    result = await db.execute(
        select(Medication).where(Medication.user_id == user_id, Medication.status == "active")
    )
    medications = result.scalars().all()

    stats = []
    for med in medications:
        sent = await db.scalar(
            select(func.count())
            .select_from(ReminderEvent)
            .where(
                ReminderEvent.medication_id == med.id,
                ReminderEvent.type == "dose",
                ReminderEvent.status == "sent",
            )
        ) or 0
        taken = await db.scalar(
            select(func.count())
            .select_from(DoseLog)
            .where(DoseLog.medication_id == med.id, DoseLog.status == "taken")
        ) or 0
        skipped = await db.scalar(
            select(func.count())
            .select_from(DoseLog)
            .where(DoseLog.medication_id == med.id, DoseLog.status == "skipped")
        ) or 0

        pct = round((taken / sent) * 100) if sent > 0 else None
        stats.append({
            "name": med.name,
            "taken": taken,
            "skipped": skipped,
            "missed": max(0, sent - taken - skipped),
            "total": sent,
            "adherence_pct": pct,
        })

    return {"medications": stats}
