"""Polling-based scheduler: dispatches pending reminder events every N seconds."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Medication, ReminderEvent, User
from app.services.notification_service import send_message, send_message_with_buttons
from app.services.reminder_service import complete_finished_courses
from app.telegram_format import esc

logger = logging.getLogger(__name__)


async def _process_due_events(db: AsyncSession) -> None:
    """Send every due reminder exactly once.

    The FOR UPDATE SKIP LOCKED row locks are what stop two workers sending the
    same reminder, and they are only held until this transaction commits — so
    there is exactly one commit, at the end, after every event is handled.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ReminderEvent)
        .where(ReminderEvent.trigger_time <= now, ReminderEvent.status == "pending")
        .with_for_update(skip_locked=True)
    )
    events = result.scalars().all()
    if events:
        logger.info("Processing %d due reminder events", len(events))

    for event in events:
        try:
            user = (
                await db.execute(select(User).where(User.id == event.user_id))
            ).scalar_one_or_none()
            if user is None:
                logger.warning("User not found for event %s", event.id)
                continue

            medication = (
                await db.execute(
                    select(Medication).where(Medication.id == event.medication_id)
                )
            ).scalar_one_or_none()
            med_name = esc(medication.name) if medication else "your medication"

            # A dose belongs to a moment. Delivering one hours or months late
            # invites the user to take a dose they have missed, and tapping
            # "Taken" would decrement stock and count toward adherence.
            # Refill alerts are a state rather than a moment, so they still stand.
            lateness = now - event.trigger_time
            if event.type == "dose" and lateness > timedelta(
                minutes=settings.reminder_max_lateness_minutes
            ):
                logger.warning(
                    "Retiring stale dose reminder for %s — %s late", med_name, lateness
                )
                event.status = "sent"
                continue

            # Skip if the medication is paused or stopped (treat NULL as active)
            if medication and medication.status and medication.status != "active":
                if (
                    medication.status == "paused"
                    and medication.paused_until
                    and medication.paused_until <= now
                ):
                    # The pause has elapsed — resume and send this reminder.
                    logger.info("Auto-resuming %s", medication.id)
                    medication.status = "active"
                    medication.paused_until = None
                else:
                    logger.info(
                        "Skipping event for %s — status: %s", med_name, medication.status
                    )
                    # Retire it so it does not keep coming back every poll.
                    event.status = "sent"
                    continue

            if event.type == "dose":
                text = f"💊 Time to take your medication: <b>{med_name}</b>"
                buttons = [[
                    {"text": "✅ Taken", "callback_data": f"taken:{event.id}"},
                    {"text": "❌ Skipped", "callback_data": f"skipped:{event.id}"},
                    {"text": "⏰ Snooze 15min", "callback_data": f"snooze:{event.id}"},
                ]]
            else:
                text = f"⚠️ <b>{med_name}</b> is running low. Reorder soon!"
                buttons = [[
                    {"text": "🛒 Add to Cart", "callback_data": f"cart_add:{event.medication_id}"},
                    {"text": "🛍️ Buy Now", "callback_data": f"buy_now:{event.medication_id}"},
                ]]

            logger.info(
                "Sending %s reminder to %s for %s", event.type, user.telegram_id, med_name
            )
            success = await send_message_with_buttons(user.telegram_id, text, buttons)

            if success:
                event.status = "sent"
                # Stock is decremented when the user taps "Taken", not here — we
                # do not yet know whether they will take or skip the dose.
            else:
                logger.warning(
                    "Delivery failed for event %s — left pending for the next poll",
                    event.id,
                )
        except Exception:
            logger.exception("Error processing event %s", event.id)

    await db.commit()


async def _announce_finished_courses(db: AsyncSession) -> None:
    """Tell users when a finite course has run its last dose."""
    for medication in await complete_finished_courses(db):
        user = await db.get(User, medication.user_id)
        if user is None:
            continue
        await send_message(
            user.telegram_id,
            f"✅ That's the end of your course of <b>{esc(medication.name)}</b>. "
            f"Reminders have stopped. If your doctor extended it, tell me "
            f"\"take {esc(medication.name)} for 3 more days\".",
        )


async def run_poll_cycle(db: AsyncSession) -> None:
    """One scheduler tick: send what is due, then retire finished courses."""
    await _process_due_events(db)
    await _announce_finished_courses(db)
