"""Finite medication courses: a 14-day course must stop after 14 days."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Medication, ReminderEvent
from app.routers.webhook import _route_intent
from app.schemas import AddMedicationIntent
from app.services import medication_service, reminder_service


async def _dose_count(db, med):
    return await db.scalar(
        select(func.count()).select_from(ReminderEvent).where(
            ReminderEvent.medication_id == med.id, ReminderEvent.type == "dose"
        )
    )


async def test_duration_sets_a_course_end(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )

    assert med.course_end is not None
    expected = datetime.now(timezone.utc) + timedelta(days=5)
    assert abs((med.course_end - expected).total_seconds()) < 120


async def test_medication_with_no_duration_has_no_course_end(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Vitamin D", ["morning"], 30
    )

    assert med.course_end is None, "ongoing medications must not expire"


async def test_a_five_day_course_generates_exactly_five_doses(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )

    await reminder_service.regenerate_dose_events(db, med)

    assert await _dose_count(db, med) == 5


async def test_no_events_are_generated_past_the_course_end(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=3
    )

    await reminder_service.regenerate_dose_events(db, med)

    events = (await db.execute(
        select(ReminderEvent).where(ReminderEvent.medication_id == med.id)
    )).scalars().all()
    assert events
    assert all(e.trigger_time < med.course_end for e in events)


async def test_a_course_longer_than_the_window_is_capped_by_the_window(db, user):
    """A 14-day course still only queues the precompute window; the daily
    top-up carries it the rest of the way."""
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 14, duration_days=14
    )

    await reminder_service.regenerate_dose_events(db, med)

    count = await _dose_count(db, med)
    assert count <= settings.reminder_precompute_days, "must not exceed the window"
    assert count < 14, "the whole course must not be queued up front"
    assert count > 0


async def test_top_up_does_not_extend_past_the_course_end(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 3, duration_days=3
    )
    await reminder_service.regenerate_dose_events(db, med)

    await reminder_service.top_up_dose_events(db)

    events = (await db.execute(
        select(ReminderEvent).where(ReminderEvent.medication_id == med.id)
    )).scalars().all()
    assert all(e.trigger_time < med.course_end for e in events)
    assert len(events) == 3


async def test_top_up_skips_a_medication_whose_course_has_already_ended(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    med.course_end = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    await reminder_service.top_up_dose_events(db)

    assert await _dose_count(db, med) == 0


async def test_a_finished_course_is_marked_completed(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    med.course_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    completed = await reminder_service.complete_finished_courses(db)

    assert [m.name for m in completed] == ["Azithro"]
    await db.refresh(med)
    assert med.status == "completed"


async def test_an_ongoing_medication_is_never_completed(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Vitamin D", ["morning"], 30
    )

    completed = await reminder_service.complete_finished_courses(db)

    assert completed == []
    await db.refresh(med)
    assert med.status == "active"


async def test_the_user_is_told_when_a_course_finishes(db, user, sent):
    from app.scheduler import run_poll_cycle

    med = await medication_service.add_or_update_medication(
        db, user.id, "Vitamin B<3", ["morning"], 5, duration_days=5
    )
    med.course_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as poll_db:
        await run_poll_cycle(poll_db)

    texts = [t for _, t in sent["messages"]]
    assert any("B&lt;3" in t for t in texts), texts
    assert not any("B<3" in t for t in texts), "the name must be HTML-escaped"


async def test_completing_a_course_retires_its_leftover_reminders(db, user):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    await reminder_service.regenerate_dose_events(db, med)
    med.course_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    await reminder_service.complete_finished_courses(db)

    left = await db.scalar(
        select(func.count()).select_from(ReminderEvent).where(
            ReminderEvent.medication_id == med.id, ReminderEvent.status == "pending"
        )
    )
    assert left == 0


async def test_add_medication_intent_carrying_a_duration_creates_a_finite_course(db, user, sent):
    intent = AddMedicationIntent(
        medicine_name="Azithro", dosage_times=["morning"], total_quantity=5, duration_days=5
    )

    reply = await _route_intent(db, user, intent, "42")

    med = await medication_service.get_medication(db, user.id, "Azithro")
    assert med.course_end is not None
    assert await _dose_count(db, med) == 5
    assert "5 day" in reply, reply


async def test_a_finished_course_is_announced_only_once(db, user, sent):
    """The active→completed transition is what stops re-announcing; course_end
    alone stays true forever."""
    from app.database import AsyncSessionLocal
    from app.scheduler import run_poll_cycle

    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    med.course_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    for _ in range(3):
        async with AsyncSessionLocal() as poll_db:
            await run_poll_cycle(poll_db)

    announcements = [t for _, t in sent["messages"] if "Azithro" in t]
    assert len(announcements) == 1, f"announced {len(announcements)} times"


async def test_concurrent_pollers_announce_a_finished_course_once(db, user, sent):
    """Two workers polling at the same moment must not both announce."""
    import asyncio

    from app.database import AsyncSessionLocal
    from app.scheduler import run_poll_cycle

    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    med.course_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    async def cycle():
        async with AsyncSessionLocal() as poll_db:
            await run_poll_cycle(poll_db)

    await asyncio.gather(cycle(), cycle())

    announcements = [t for _, t in sent["messages"] if "Azithro" in t]
    assert len(announcements) == 1, f"announced {len(announcements)} times"


async def test_readding_a_completed_medication_reactivates_it(db, user, sent):
    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    med.status = "completed"
    med.course_end = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=3
    )

    assert med.status == "active", "adding it again must restart it"
    assert med.course_end > datetime.now(timezone.utc)


async def test_resuming_a_completed_course_does_not_claim_it_resumed(db, user, sent):
    from app.schemas import ResumeMedicationIntent

    med = await medication_service.add_or_update_medication(
        db, user.id, "Azithro", ["morning"], 5, duration_days=5
    )
    med.status = "completed"
    med.course_end = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    reply = await _route_intent(db, user, ResumeMedicationIntent(medicine_name="Azithro"), "42")

    assert "resumed" not in reply.lower(), f"misleading reply: {reply}"
    assert "complete" in reply.lower(), reply
