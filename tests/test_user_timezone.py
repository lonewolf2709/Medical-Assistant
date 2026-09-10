"""Reminders must fire on each user's own clock, not one process-wide clock."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Medication, ReminderEvent, User
from app.routers.webhook import _route_intent
from app.services import medication_service, reminder_service


async def _trigger_times_utc(db, med):
    rows = (await db.execute(
        select(ReminderEvent.trigger_time).where(
            ReminderEvent.medication_id == med.id, ReminderEvent.type == "dose"
        ).order_by(ReminderEvent.trigger_time)
    )).scalars().all()
    return [t.astimezone(timezone.utc).strftime("%H:%M") for t in rows]


async def test_a_user_with_no_timezone_falls_back_to_the_configured_default(db, user):
    assert user.timezone is None

    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    await reminder_service.regenerate_dose_events(db, med)

    # Asia/Kolkata is UTC+5:30, so an 08:00 local dose is 02:30 UTC.
    assert set(await _trigger_times_utc(db, med)) == {"02:30"}


async def test_reminders_use_the_users_own_timezone(db, user):
    user.timezone = "Asia/Karachi"  # UTC+5, so 08:00 local is 03:00 UTC
    await db.commit()

    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    await reminder_service.regenerate_dose_events(db, med)

    assert set(await _trigger_times_utc(db, med)) == {"03:00"}


async def test_two_users_in_different_timezones_get_different_utc_triggers(db, user):
    other = User(telegram_id="99", timezone="Europe/London")
    db.add(other)
    await db.commit()
    await db.refresh(other)

    mine = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    theirs = await medication_service.add_or_update_medication(db, other.id, "Crocin", ["morning"], 30)
    await reminder_service.regenerate_dose_events(db, mine)
    await reminder_service.regenerate_dose_events(db, theirs)

    assert await _trigger_times_utc(db, mine) != await _trigger_times_utc(db, theirs)


async def test_the_daily_top_up_also_respects_each_users_timezone(db, user):
    user.timezone = "Asia/Karachi"
    await db.commit()
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)

    await reminder_service.top_up_dose_events(db)

    assert set(await _trigger_times_utc(db, med)) == {"03:00"}


async def test_setting_a_timezone_is_saved(db, user, sent):
    from app.schemas import SetTimezoneIntent

    reply = await _route_intent(db, user, SetTimezoneIntent(timezone="Asia/Karachi"), "42")

    await db.refresh(user)
    assert user.timezone == "Asia/Karachi"
    assert "Asia/Karachi" in reply


async def test_an_unknown_timezone_is_rejected_without_saving(db, user, sent):
    from app.schemas import SetTimezoneIntent

    reply = await _route_intent(db, user, SetTimezoneIntent(timezone="Mars/Olympus"), "42")

    await db.refresh(user)
    assert user.timezone is None, "an invalid timezone must not be stored"
    assert "didn't recognise" in reply.lower() or "not recognise" in reply.lower(), reply


async def test_changing_timezone_reschedules_existing_reminders(db, user, sent):
    from app.schemas import SetTimezoneIntent

    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    await reminder_service.regenerate_dose_events(db, med)
    assert set(await _trigger_times_utc(db, med)) == {"02:30"}

    await _route_intent(db, user, SetTimezoneIntent(timezone="Asia/Karachi"), "42")

    assert set(await _trigger_times_utc(db, med)) == {"03:00"}, (
        "queued reminders must move with the user"
    )


async def test_the_medication_list_renders_times_in_the_users_timezone(db, user, sent):
    from app.schemas import ListMedicationsIntent

    user.timezone = "Europe/London"
    await db.commit()
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["09:00"], 30)
    await reminder_service.regenerate_dose_events(db, med)

    reply = await _route_intent(db, user, ListMedicationsIntent(), "42")

    assert "09:00 AM" in reply or "09:00" in reply, reply
