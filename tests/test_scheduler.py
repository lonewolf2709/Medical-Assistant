"""Reminder dispatch and the rolling top-up of precomputed reminder events."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Medication, ReminderEvent
from app.scheduler import _process_due_events
from app.services import medication_service


async def _count_dose_events(db, med):
    return await db.scalar(
        select(func.count()).select_from(ReminderEvent).where(
            ReminderEvent.medication_id == med.id, ReminderEvent.type == "dose"
        )
    )


async def test_dose_events_run_out_are_topped_up_for_the_window(db, user, sent):
    from app.services.reminder_service import top_up_dose_events

    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning", "night"], 100)
    # Simulate the week of precomputed events having been used up.
    await db.execute(delete(ReminderEvent).where(ReminderEvent.medication_id == med.id))
    await db.commit()
    assert await _count_dose_events(db, med) == 0

    topped = await top_up_dose_events(db)

    assert topped == 1, "one medication should have been topped up"
    count = await _count_dose_events(db, med)
    assert count >= settings.reminder_precompute_days, f"only {count} events created"


async def test_top_up_does_not_duplicate_existing_events(db, user, sent):
    from app.services.reminder_service import top_up_dose_events

    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 100)

    await top_up_dose_events(db)
    after_first = await _count_dose_events(db, med)
    await top_up_dose_events(db)

    assert after_first > 0
    assert await _count_dose_events(db, med) == after_first, "top-up must be idempotent"


async def test_top_up_skips_paused_and_stopped_medications(db, user, sent):
    from app.services.reminder_service import top_up_dose_events

    paused = await medication_service.add_or_update_medication(db, user.id, "Paused", ["morning"], 100)
    stopped = await medication_service.add_or_update_medication(db, user.id, "Stopped", ["morning"], 100)
    await medication_service.pause_medication(db, user.id, "Paused", 3)
    await medication_service.stop_medication(db, user.id, "Stopped")

    await top_up_dose_events(db)

    assert await _count_dose_events(db, paused) == 0
    assert await _count_dose_events(db, stopped) == 0


async def test_due_dose_event_is_sent_and_marked_sent(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    event = ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=1),
        type="dose", status="pending",
    )
    db.add(event)
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    await db.refresh(event)
    assert event.status == "sent"
    assert len(sent["buttons"]) == 1


async def test_reminder_text_escapes_the_medication_name(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Vitamin B<3", ["morning"], 30)
    db.add(ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=1),
        type="dose", status="pending",
    ))
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    _, text, _ = sent["buttons"][0]
    assert "B&lt;3" in text
    assert "B<3" not in text


async def test_concurrent_pollers_send_each_due_event_exactly_once(db, user, sent):
    """Committing mid-loop releases the FOR UPDATE locks early, which lets a second
    poller pick up rows the first one is still working through."""
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    paused = await medication_service.add_or_update_medication(db, user.id, "Paused", ["morning"], 30)
    await medication_service.pause_medication(db, user.id, "Paused", 3)
    await db.refresh(paused)
    # Force the auto-resume branch, which is where the mid-loop commit happens.
    paused.paused_until = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    now = datetime.now(timezone.utc)
    for target in (paused, med, med, med):
        db.add(ReminderEvent(
            user_id=user.id, medication_id=target.id,
            trigger_time=now - timedelta(minutes=1), type="dose", status="pending",
        ))
    await db.commit()

    import app.scheduler as scheduler_mod

    original = scheduler_mod.send_message_with_buttons

    async def slow_send(chat_id, text, buttons):
        await asyncio.sleep(0.05)  # widen the window between events
        return await original(chat_id, text, buttons)

    scheduler_mod.send_message_with_buttons = slow_send
    try:
        async def poll():
            async with AsyncSessionLocal() as poll_db:
                await _process_due_events(poll_db)

        await asyncio.gather(poll(), poll())
    finally:
        scheduler_mod.send_message_with_buttons = original

    # Every due event must be delivered exactly once, with no event sent twice.
    delivered = [
        b["callback_data"].split(":", 1)[1]
        for _, _, buttons in sent["buttons"]
        for row in buttons
        for b in row
        if b["callback_data"].startswith("taken:")
    ]
    assert len(delivered) == len(set(delivered)), f"duplicate reminders sent: {delivered}"
    assert len(delivered) == 4, f"expected all 4 due events delivered, got {len(delivered)}"

    remaining = await db.scalar(
        select(func.count()).select_from(ReminderEvent).where(ReminderEvent.status == "pending")
    )
    assert remaining == 0


async def test_a_reminder_that_is_hours_late_is_not_delivered(db, user, sent):
    """A 60-day-late dose reminder must not arrive as if it were due now —
    tapping Taken on it would decrement stock and skew adherence."""
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    event = ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(days=60),
        type="dose", status="pending",
    )
    db.add(event)
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    assert sent["buttons"] == [], "a stale reminder must not be delivered"


async def test_a_stale_reminder_is_retired_so_it_stops_coming_back(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    event = ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(days=60),
        type="dose", status="pending",
    )
    db.add(event)
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    await db.refresh(event)
    assert event.status == "sent", "it must not stay pending and retry forever"


async def test_a_reminder_only_slightly_late_is_still_delivered(db, user, sent):
    """The poll runs once a minute, so a few minutes of lateness is normal."""
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    db.add(ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=3),
        type="dose", status="pending",
    ))
    await db.commit()

    async with AsyncSessionLocal() as poll_db:
        await _process_due_events(poll_db)

    assert len(sent["buttons"]) == 1
