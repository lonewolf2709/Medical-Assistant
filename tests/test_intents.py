"""Intent routing, reply formatting, adherence math and medication lookup."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import DoseLog, Medication, ReminderEvent
from app.schemas import AddMedicationIntent, ListMedicationsIntent
from app.services import medication_service, message_log_service, reminder_service
from app.routers.webhook import _handle_voice, _route_intent

HOSTILE_NAME = "Vitamin B<3 & Co"


async def test_medication_name_with_html_characters_is_escaped_in_the_reply(db, user, sent):
    intent = AddMedicationIntent(medicine_name=HOSTILE_NAME, dosage_times=["morning"], total_quantity=10)

    reply = await _route_intent(db, user, intent, "42")

    assert "B&lt;3 &amp; Co" in reply, reply
    assert "B<3" not in reply, "raw < in HTML-parsed text makes Telegram reject the message"


async def test_medication_list_escapes_names(db, user, sent):
    await medication_service.add_or_update_medication(db, user.id, HOSTILE_NAME, ["morning"], 10)

    reply = await _route_intent(db, user, ListMedicationsIntent(), "42")

    assert "B&lt;3 &amp; Co" in reply, reply
    assert "B<3" not in reply


async def test_voice_message_is_transcribed_and_answered(db, user, sent, monkeypatch):
    import app.routers.webhook as webhook_mod

    async def fake_bytes(file_id):
        return b"audio"

    async def fake_transcribe(audio):
        return "show my medications"

    async def fake_parse(text):
        return [ListMedicationsIntent()]

    monkeypatch.setattr(webhook_mod, "get_voice_bytes", fake_bytes)
    monkeypatch.setattr(webhook_mod, "transcribe", fake_transcribe)
    monkeypatch.setattr(webhook_mod, "parse_intent", fake_parse)

    class Voice:
        duration = 5
        file_id = "voice-1"

    await _handle_voice(db, user, "42", Voice())

    assert sent["messages"], "a voice message must produce a reply"


async def test_adherence_counts_reminders_the_user_ignored_as_missed(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    events = []
    for i in range(3):
        e = ReminderEvent(
            user_id=user.id, medication_id=med.id,
            trigger_time=datetime.now(timezone.utc) - timedelta(hours=i + 1),
            type="dose", status="sent",
        )
        db.add(e)
        events.append(e)
    await db.flush()
    db.add(DoseLog(user_id=user.id, medication_id=med.id,
                   reminder_event_id=events[0].id, status="taken"))
    await db.commit()

    stats = await medication_service.get_adherence_stats(db, user.id)
    row = next(m for m in stats["medications"] if m["name"] == "Crocin")

    assert row["total"] == 3, "denominator must be reminders sent, not reminders answered"
    assert row["taken"] == 1
    assert row["adherence_pct"] == 33


async def test_adherence_ignores_reminders_not_yet_sent(db, user, sent):
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 30)
    db.add(ReminderEvent(
        user_id=user.id, medication_id=med.id,
        trigger_time=datetime.now(timezone.utc) + timedelta(days=1),
        type="dose", status="pending",
    ))
    await db.commit()

    stats = await medication_service.get_adherence_stats(db, user.id)
    row = next(m for m in stats["medications"] if m["name"] == "Crocin")

    assert row["total"] == 0
    assert row["adherence_pct"] is None


async def test_refill_alert_is_scheduled_immediately_when_stock_is_already_low(db, user, sent):
    # 2 tablets left at 1/day is inside the 5-day alert window already.
    med = await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 2)

    await reminder_service.regenerate_refill_event(db, med)

    events = (await db.execute(
        select(ReminderEvent).where(
            ReminderEvent.medication_id == med.id, ReminderEvent.type == "refill"
        )
    )).scalars().all()
    assert len(events) == 1, "a user who is already low must still be warned"
    assert events[0].trigger_time <= datetime.now(timezone.utc) + timedelta(minutes=1)


async def test_medication_lookup_does_not_treat_the_name_as_a_sql_wildcard(db, user):
    await medication_service.add_or_update_medication(db, user.id, "Crocin", ["morning"], 10)

    assert await medication_service.get_medication(db, user.id, "Croc%") is None
    assert await medication_service.get_medication(db, user.id, "crocin") is not None


async def test_pending_action_is_ignored_once_it_has_expired(db, user):
    await message_log_service.log_message(db, user.id, "pause crocin")
    await message_log_service.set_pending_action(
        db, user.id, {"action": "pause_medication", "medicine_name": "Crocin"}
    )

    assert await message_log_service.get_pending_action(db, user.id) is not None

    # An abandoned follow-up must not hijack the user's next message hours later.
    assert await message_log_service.get_pending_action(
        db, user.id, now=datetime.now(timezone.utc) + timedelta(hours=3)
    ) is None
