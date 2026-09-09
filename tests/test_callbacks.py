"""Inline-button callback handling: dose logging, idempotency, ownership, cart buttons."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import CartItem, DoseLog, Medication, ReminderEvent, User
from app.routers.webhook import _process_callback_inner


async def _make_med_and_event(db, user, *, kind="dose", remaining=10.0, daily_dose=1.0):
    med = Medication(
        user_id=user.id, name="Crocin", total_quantity=remaining,
        remaining_quantity=remaining, daily_dose=daily_dose, status="active",
    )
    db.add(med)
    await db.flush()
    event = ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) - timedelta(minutes=1),
        type=kind, status="sent",
    )
    db.add(event)
    await db.commit()
    await db.refresh(med)
    await db.refresh(event)
    return med, event


def _callback(data, *, telegram_id="42", message_id=7):
    return {
        "id": "cb-1",
        "data": data,
        "from": {"id": int(telegram_id)},
        "message": {"message_id": message_id, "chat": {"id": int(telegram_id)}},
    }


async def test_taken_logs_the_dose_and_decrements_stock(db, user, sent):
    med, event = await _make_med_and_event(db, user, remaining=10.0)

    await _process_callback_inner(_callback(f"taken:{event.id}"))

    logs = (await db.execute(select(DoseLog).where(DoseLog.reminder_event_id == event.id))).scalars().all()
    assert [l.status for l in logs] == ["taken"]
    await db.refresh(med)
    assert med.remaining_quantity == 9.0


async def test_tapping_taken_twice_records_one_dose_and_decrements_once(db, user, sent):
    med, event = await _make_med_and_event(db, user, remaining=10.0)

    await _process_callback_inner(_callback(f"taken:{event.id}"))
    await _process_callback_inner(_callback(f"taken:{event.id}"))

    count = await db.scalar(select(func.count()).select_from(DoseLog).where(DoseLog.reminder_event_id == event.id))
    assert count == 1, "a repeated tap must not create a second dose log"
    await db.refresh(med)
    assert med.remaining_quantity == 9.0, "a repeated tap must not decrement stock again"


async def test_skipped_is_also_idempotent(db, user, sent):
    med, event = await _make_med_and_event(db, user)

    await _process_callback_inner(_callback(f"skipped:{event.id}"))
    await _process_callback_inner(_callback(f"skipped:{event.id}"))

    count = await db.scalar(select(func.count()).select_from(DoseLog).where(DoseLog.reminder_event_id == event.id))
    assert count == 1


async def test_callback_from_another_user_cannot_log_someone_elses_dose(db, user, sent):
    med, event = await _make_med_and_event(db, user, remaining=10.0)

    await _process_callback_inner(_callback(f"taken:{event.id}", telegram_id="99"))

    count = await db.scalar(select(func.count()).select_from(DoseLog))
    assert count == 0, "an event belonging to another user must be rejected"
    await db.refresh(med)
    assert med.remaining_quantity == 10.0


async def test_snooze_reschedules_the_event_to_pending(db, user, sent):
    med, event = await _make_med_and_event(db, user)
    before = datetime.now(timezone.utc)

    await _process_callback_inner(_callback(f"snooze:{event.id}"))

    await db.refresh(event)
    assert event.status == "pending"
    assert event.trigger_time > before + timedelta(minutes=10)


async def test_cart_add_button_on_a_refill_reminder_adds_to_cart(db, user, sent):
    med, event = await _make_med_and_event(db, user, kind="refill")

    await _process_callback_inner(_callback(f"cart_add:{med.id}"))

    items = (await db.execute(select(CartItem).where(CartItem.user_id == user.id))).scalars().all()
    assert [(i.medication_id, i.status) for i in items] == [(med.id, "active")]


async def test_buy_now_button_sends_price_links(db, user, sent, monkeypatch):
    med, event = await _make_med_and_event(db, user, kind="refill")

    async def fake_prices(name):
        return {"1mg": "₹45", "PharmEasy": "₹48", "Apollo": "₹50"}

    import app.services.cart_service as cart_service

    monkeypatch.setattr(cart_service, "_fetch_single_price", fake_prices)

    await _process_callback_inner(_callback(f"buy_now:{med.id}"))

    assert any("Crocin" in text for _, text in sent["messages"]), sent["messages"]


async def test_cart_add_for_a_medication_owned_by_someone_else_is_rejected(db, user, sent):
    other = User(telegram_id="99")
    db.add(other)
    await db.flush()
    med = Medication(user_id=other.id, name="Crocin", total_quantity=10,
                     remaining_quantity=10, daily_dose=1, status="active")
    db.add(med)
    await db.commit()

    await _process_callback_inner(_callback(f"cart_add:{med.id}", telegram_id="42"))

    count = await db.scalar(select(func.count()).select_from(CartItem))
    assert count == 0


async def test_unparseable_callback_data_is_ignored(db, user, sent):
    await _process_callback_inner(_callback("garbage-with-no-colon"))
    await _process_callback_inner(_callback(f"taken:{uuid.uuid4()}"))

    count = await db.scalar(select(func.count()).select_from(DoseLog))
    assert count == 0
