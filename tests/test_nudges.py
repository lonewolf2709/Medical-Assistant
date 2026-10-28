"""An ignored dose reminder gets one follow-up nudge — the point of a reminder app."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DoseLog, ReminderEvent
from app.services import medication_service, reminder_service


async def _sent_dose(db, user, name="Crocin", minutes_ago=45):
    med = await medication_service.add_or_update_medication(db, user.id, name, ["morning"], 30)
    event = ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        type="dose", status="sent",
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return med, event


async def _nudge_texts(sent):
    return [text for _, text, _ in sent["buttons"]]


async def test_an_unanswered_reminder_is_nudged_after_the_wait(db, user, sent):
    med, event = await _sent_dose(db, user, minutes_ago=settings.dose_nudge_after_minutes + 5)

    nudged = await reminder_service.nudge_unanswered_doses(db)

    assert [e.id for e in nudged] == [event.id]


async def test_a_reminder_is_not_nudged_before_the_wait(db, user, sent):
    await _sent_dose(db, user, minutes_ago=settings.dose_nudge_after_minutes - 5)

    assert await reminder_service.nudge_unanswered_doses(db) == []


async def test_an_answered_reminder_is_not_nudged(db, user, sent):
    med, event = await _sent_dose(db, user)
    db.add(DoseLog(user_id=user.id, medication_id=med.id,
                   reminder_event_id=event.id, status="skipped"))
    await db.commit()

    assert await reminder_service.nudge_unanswered_doses(db) == []


async def test_a_reminder_is_nudged_at_most_once(db, user, sent):
    await _sent_dose(db, user)

    first = await reminder_service.nudge_unanswered_doses(db)
    second = await reminder_service.nudge_unanswered_doses(db)

    assert len(first) == 1
    assert second == [], "nagging every minute is worse than no reminder"


async def test_a_snoozed_reminder_is_not_nudged(db, user, sent):
    med, event = await _sent_dose(db, user)
    event.status = "pending"  # what Snooze does
    event.trigger_time = datetime.now(timezone.utc) + timedelta(minutes=15)
    await db.commit()

    assert await reminder_service.nudge_unanswered_doses(db) == []


async def test_a_reminder_ignored_for_hours_is_left_alone(db, user, sent):
    """The nudge is a gentle 'still there?', not a resurrection of yesterday's
    dose. Old unanswered reminders simply count as missed."""
    await _sent_dose(db, user, minutes_ago=settings.dose_nudge_window_minutes + 30)

    assert await reminder_service.nudge_unanswered_doses(db) == []


async def test_the_nudge_keeps_the_buttons_and_escapes_the_name(db, user, sent):
    from app.scheduler import run_poll_cycle

    med, event = await _sent_dose(db, user, name="Vitamin B<3")

    async with AsyncSessionLocal() as poll_db:
        await run_poll_cycle(poll_db)

    texts = await _nudge_texts(sent)
    assert len(texts) == 1, texts
    assert "B&lt;3" in texts[0] and "B<3" not in texts[0]
    _, _, buttons = sent["buttons"][0]
    actions = {b["callback_data"].split(":")[0] for row in buttons for b in row}
    assert actions == {"taken", "skipped", "snooze"}, "the user must still be able to answer"


async def test_tapping_taken_on_the_nudge_logs_the_original_dose(db, user, sent):
    from app.routers.webhook import _process_callback_inner
    from app.scheduler import run_poll_cycle

    med, event = await _sent_dose(db, user)
    async with AsyncSessionLocal() as poll_db:
        await run_poll_cycle(poll_db)
    _, _, buttons = sent["buttons"][0]
    taken = next(b["callback_data"] for row in buttons for b in row if b["callback_data"].startswith("taken:"))

    await _process_callback_inner({"id": "cb", "data": taken, "from": {"id": 42},
                                   "message": {"message_id": 9, "chat": {"id": 42}}})

    log = (await db.execute(select(DoseLog).where(DoseLog.reminder_event_id == event.id))).scalars().first()
    assert log is not None and log.status == "taken"
