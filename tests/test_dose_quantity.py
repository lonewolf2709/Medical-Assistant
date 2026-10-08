"""How many tablets a single dose is — "take 2 twice a day"."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import DosageTime, Medication, ReminderEvent
from app.routers.webhook import _process_callback_inner, _route_intent
from app.schemas import AddMedicationIntent
from app.services import medication_service, reminder_service


async def _dosage_times(db, med):
    return (await db.execute(
        select(DosageTime).where(DosageTime.medication_id == med.id)
    )).scalars().all()


async def test_a_dose_is_one_tablet_unless_stated(db, user):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)

    assert [d.dose_quantity for d in await _dosage_times(db, med)] == [1.0]
    assert med.daily_dose == 1.0


async def test_two_tablets_twice_a_day_is_four_tablets_a_day(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Crocin", ["morning", "night"], 40, dose_quantity=2
    )

    assert [d.dose_quantity for d in await _dosage_times(db, med)] == [2.0, 2.0]
    assert med.daily_dose == 4.0, "daily_dose must be tablets per day, not doses per day"


async def test_a_dose_event_records_which_dosage_time_it_came_from(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Crocin", ["morning"], 30, dose_quantity=2
    )
    await reminder_service.regenerate_dose_events(db, med)

    events = (await db.execute(
        select(ReminderEvent).where(ReminderEvent.medication_id == med.id)
    )).scalars().all()
    assert events
    assert all(e.dosage_time_id is not None for e in events), (
        "without this the bot cannot know how many tablets the dose was"
    )


async def test_taking_a_dose_decrements_by_the_dose_quantity(db, user, sent):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Crocin", ["morning"], 30, dose_quantity=2
    )
    await reminder_service.regenerate_dose_events(db, med)
    event = (await db.execute(
        select(ReminderEvent).where(ReminderEvent.medication_id == med.id)
    )).scalars().first()
    event.status = "sent"
    await db.commit()

    await _process_callback_inner({
        "id": "cb", "data": f"taken:{event.id}",
        "from": {"id": 42}, "message": {"message_id": 7, "chat": {"id": 42}},
    })

    await db.refresh(med)
    assert med.remaining_quantity == 28.0, "two tablets were taken, not one"


async def test_the_refill_projection_uses_the_real_daily_total(db, user):
    # 30 tablets at 2 twice a day = 4/day = 7.5 days of supply,
    # so the 5-day warning is 2.5 days out — not 25.
    med = await medication_service.add_or_update_medication(
        db, user.id, "Crocin", ["morning", "night"], 30, dose_quantity=2
    )
    await reminder_service.regenerate_refill_event(db, med)

    event = (await db.execute(
        select(ReminderEvent).where(
            ReminderEvent.medication_id == med.id, ReminderEvent.type == "refill"
        )
    )).scalars().first()
    days_out = (event.trigger_time - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 2 < days_out < 3, f"expected ~2.5 days, got {days_out:.1f}"


async def test_the_reminder_says_how_many_tablets_to_take(db, user, sent):
    from app.scheduler import _process_due_events
    from app.database import AsyncSessionLocal

    med = await medication_service.add_or_update_medication(
        db, user.id, "Crocin", ["morning"], 30, dose_quantity=2
    )
    dosage_time = (await _dosage_times(db, med))[0]
    db.add(ReminderEvent(
        user_id=user.id, medication_id=med.id, dosage_time_id=dosage_time.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=1),
        type="dose", status="pending",
    ))
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    _, text, _ = sent["buttons"][0]
    assert "2" in text, f"the user needs to know how many to take: {text}"


async def test_a_single_tablet_reminder_does_not_mention_a_count(db, user, sent):
    from app.scheduler import _process_due_events
    from app.database import AsyncSessionLocal

    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    dosage_time = (await _dosage_times(db, med))[0]
    db.add(ReminderEvent(
        user_id=user.id, medication_id=med.id, dosage_time_id=dosage_time.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=1),
        type="dose", status="pending",
    ))
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    _, text, _ = sent["buttons"][0]
    assert "Time to take your medication" in text, text


async def test_add_medication_intent_carries_the_dose_quantity(db, user, sent):
    intent = AddMedicationIntent(
        medicine_name="Crocin", dosage_times=["morning", "night"],
        total_quantity=40, dose_quantity=2,
    )

    reply = await _route_intent(db, user, intent, "42")

    med = await medication_service.get_medication(db, user.id, "Crocin")
    assert med.daily_dose == 4.0
    assert "2" in reply, reply
