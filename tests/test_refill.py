"""Refill projections: kept fresh, and never nagging."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import ReminderEvent
from app.routers.webhook import _route_intent
from app.schemas import SetReminderIntent
from app.services import medication_service, reminder_service


async def _pending_refill(db, med):
    return (await db.execute(
        select(ReminderEvent).where(
            ReminderEvent.medication_id == med.id,
            ReminderEvent.type == "refill",
            ReminderEvent.status == "pending",
        )
    )).scalars().first()


async def _sent_refill(db, med, days_ago):
    event = ReminderEvent(
        user_id=med.user_id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(days=days_ago),
        type="refill", status="sent",
    )
    db.add(event)
    await db.commit()
    return event


async def test_changing_the_dose_schedule_refreshes_the_refill_projection(db, user, sent):
    """30 tablets at 1/day runs out in 30 days; at 3/day it runs out in 10, so
    the refill alert must move much closer."""
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    await reminder_service.regenerate_refill_event(db, med)
    before = (await _pending_refill(db, med)).trigger_time

    await _route_intent(
        db, user, SetReminderIntent(medicine_name="Crocin", times=["morning", "evening", "night"]), "42"
    )

    after = (await _pending_refill(db, med)).trigger_time
    assert after < before, "tripling the daily dose must bring the refill alert forward"
    days_out = (after - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 4 < days_out < 6, f"expected ~5 days out, got {days_out:.1f}"


async def test_daily_maintenance_refreshes_the_projection_as_stock_depletes(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    await reminder_service.regenerate_refill_event(db, med)
    before = (await _pending_refill(db, med)).trigger_time

    # Simulate three weeks of doses taken without any add/refill action.
    med.remaining_quantity = 8.0
    await db.commit()

    await reminder_service.run_daily_maintenance(db)

    after = (await _pending_refill(db, med)).trigger_time
    assert after < before, "the projection must track actual stock"
    days_out = (after - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 2 < days_out < 4, f"8 tablets at 1/day → alert ~3 days out, got {days_out:.1f}"


async def test_an_already_low_medication_is_not_realerted_every_day(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 2)
    await _sent_refill(db, med, days_ago=0)

    await reminder_service.regenerate_refill_event(db, med)

    assert await _pending_refill(db, med) is None, (
        "a fresh immediate alert one day after the last one is nagging"
    )


async def test_the_realert_cooldown_expires(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 2)
    await _sent_refill(db, med, days_ago=settings.refill_realert_cooldown_days + 1)

    await reminder_service.regenerate_refill_event(db, med)

    assert await _pending_refill(db, med) is not None, "after the cooldown it must warn again"


async def test_replenishing_stock_still_schedules_a_future_alert_during_the_cooldown(db, user, sent):
    """The cooldown must only suppress immediate re-alerts — a future-dated
    projection after a refill is exactly what we want to keep."""
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 2)
    await _sent_refill(db, med, days_ago=0)

    med = await medication_service.apply_refill(db, user.id, "Crocin", 30, "tablet")
    await reminder_service.regenerate_refill_event(db, med)

    event = await _pending_refill(db, med)
    assert event is not None, "restocking must reschedule the next alert"
    assert event.trigger_time > datetime.now(timezone.utc) + timedelta(days=20)


def test_the_daily_task_runs_both_top_up_and_refill_refresh():
    import inspect

    import app.tasks

    source = inspect.getsource(app.tasks.top_up_reminders)
    assert "run_daily_maintenance" in source, source
