import logging
import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.rate_limiter import limiter
from app.schemas import ConvesationIntent, QnaIntent, TelegramUpdate
from app.services import medication_service, message_log_service, qa_service, reminder_service
from app.services.notification_service import (
    delete_message,
    edit_message,
    send_message,
    send_message_get_id,
    send_typing,
)
from app.services.parser import parse_intent
from app.telegram_format import esc
from app.services.prescription_service import download_and_store, extract_medications
from app.services.user_service import get_or_create_user
from app.services.voice_service import get_voice_bytes, transcribe

logger = logging.getLogger(__name__)
router = APIRouter()

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


_esc = esc


def _secret_is_valid(token: str | None) -> bool:
    """Constant-time comparison against the configured webhook secret.

    Fails closed: with no secret configured every request is rejected, because
    an unauthenticated webhook lets anyone read and write any user's data.
    """
    expected = settings.telegram_webhook_secret
    if not expected:
        return False
    return secrets.compare_digest(token or "", expected)


@router.post("/webhook")
@limiter.limit("30/minute")
async def telegram_webhook(
    request: Request,
    update: dict,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Authenticate, acknowledge Telegram immediately, process in the background."""
    if not _secret_is_valid(x_telegram_bot_api_secret_token):
        logger.warning(
            "Rejected webhook call with missing or invalid secret token",
            extra={"client": request.client.host if request.client else "unknown"},
        )
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    # Handle callback_query (button presses)
    if update.get("callback_query"):
        background.add_task(_process_callback, update["callback_query"])
        return {"ok": True}

    # Handle regular messages
    try:
        msg_update = TelegramUpdate(**update)
    except Exception:
        return {"ok": True}

    message = msg_update.message
    if not message or not message.from_:
        return {"ok": True}

    background.add_task(_process_message, msg_update)
    return {"ok": True}



async def _process_message(update: TelegramUpdate) -> None:
    """Full message processing in background — Telegram already got its 200."""
    try:
        await _process_message_inner(update)
    except Exception:
        logger.exception("Unhandled error in _process_message")


async def _process_message_inner(update: TelegramUpdate) -> None:
    message = update.message
    telegram_id = str(message.from_.id)
    chat_id = str(message.chat.id)

    async with AsyncSessionLocal() as db:
        try:
            user = await get_or_create_user(db, telegram_id)
        except Exception:
            logger.exception("Failed to get or create user %s", telegram_id)
            await send_message(chat_id, "Something went wrong. Please try again.")
            return

        # Handle prescription image upload
        if message.photo:
            await _handle_photo(db, user, chat_id, message.photo)
            return

        # Handle voice message
        if message.voice:
            await _handle_voice(db, user, chat_id, message.voice)
            return

        raw_text = message.text or ""
        if not raw_text:
            return

        await send_typing(chat_id)

        # Check for pending follow-up action first
        pending = await message_log_service.get_pending_action(db, user.id)
        if pending:
            await message_log_service.clear_pending_action(db, user.id)
            reply = await _handle_pending_action(db, user, pending, raw_text)
            placeholder_id = await send_message_get_id(chat_id, "💭 <i>Thinking...</i>")
            await edit_message(chat_id, placeholder_id, reply)
            return

        # Check for prescription confirmation
        if raw_text.strip().lower() in ("confirm", "yes", "add them", "add all"):
            from app.services.prescription_service import get_pending_prescription
            pending_meds = await get_pending_prescription(db, user.id)
            if pending_meds:
                added = []
                for m in pending_meds:
                    med = await medication_service.add_or_update_medication(
                        db, user.id,
                        m.get("medicine_name", ""),
                        m.get("dosage_times", []),
                        m.get("total_quantity")
                    )
                    await reminder_service.regenerate_dose_events(db, med)
                    await reminder_service.regenerate_refill_event(db, med)
                    added.append(med.name)
                reply = f"✅ Added {len(added)} medication(s): <b>{_esc(', '.join(added))}</b>. Reminders are set."
                await send_message(chat_id, reply)
                return

        # Send placeholder immediately so user sees something right away
        placeholder_id = await send_message_get_id(chat_id, "💭 <i>Thinking...</i>")

        try:
            msg_record = await message_log_service.log_message(db, user.id, raw_text)
        except Exception:
            logger.exception("Failed to log message for user %s", telegram_id)
            msg_record = None

        intents = await parse_intent(raw_text)

        if msg_record:
            try:
                await message_log_service.update_parsed_intent(db, msg_record, intents)
            except Exception:
                logger.exception("Failed to update parsed intent")

        replies = []
        non_streamed_replies = []
        for intent in intents:
            reply = await _route_intent(db, user, intent, chat_id, placeholder_id)
            replies.append(reply)
            if not isinstance(intent, (QnaIntent, ConvesationIntent)):
                non_streamed_replies.append(reply)
            else:
                placeholder_id = None  # streamed intent consumed the placeholder

        # Edit placeholder with combined non-streamed replies
        if non_streamed_replies and placeholder_id:
            await edit_message(chat_id, placeholder_id, "\n\n".join(non_streamed_replies))
        elif placeholder_id and not non_streamed_replies:
            # Shouldn't happen now but safety net
            await delete_message(chat_id, placeholder_id)

        combined_reply = "\n\n".join(replies)

        if msg_record:
            try:
                await message_log_service.store_bot_reply(db, msg_record, combined_reply)
            except Exception:
                logger.exception("Failed to store bot reply")


async def _route_intent(db, user, intent, chat_id: str, placeholder_id: int | None = None) -> str:
    from app.schemas import (AddMedicationIntent, SetReminderIntent, RefillIntent, QnaIntent,
                              ListMedicationsIntent, ConvesationIntent, StopMedicationIntent,
                              PauseMedicationIntent, ResumeMedicationIntent, AdherenceIntent,
                              AddToCartIntent, ViewCartIntent, RemoveFromCartIntent,
                              CheckoutIntent, OrderConfirmedIntent, UnknownIntent)
    from app.services.conversation_service import respond as conversation_respond
    from app.services import cart_service

    # Load chat history for contextual intents
    history = None
    if isinstance(intent, (QnaIntent, ConvesationIntent)):
        try:
            history = await message_log_service.get_recent_history(db, user.id)
        except Exception:
            logger.exception("Failed to load chat history")

    match intent:
        case AddMedicationIntent():
            if not intent.medicine_name:
                return "What is the name of the medication you'd like to add?"
            med = await medication_service.add_or_update_medication(
                db, user.id, intent.medicine_name, intent.dosage_times, intent.total_quantity
            )
            await reminder_service.regenerate_dose_events(db, med)
            await reminder_service.regenerate_refill_event(db, med)
            times = _esc(", ".join(intent.dosage_times))
            return f"✅ Added <b>{_esc(med.name)}</b> with reminders at: {times}"

        case SetReminderIntent():
            from app.services.medication_service import get_medication
            med = await get_medication(db, user.id, intent.medicine_name)
            if med is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b>. Try adding it first."
            med = await medication_service.add_or_update_medication(
                db, user.id, intent.medicine_name, intent.times, None
            )
            await reminder_service.regenerate_dose_events(db, med)
            times = _esc(", ".join(intent.times))
            return f"✅ Updated reminders for <b>{_esc(med.name)}</b> to: {times}"

        case RefillIntent():
            med = await medication_service.apply_refill(
                db, user.id, intent.medicine_name, intent.quantity, intent.unit
            )
            if med is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b>."
            await reminder_service.regenerate_refill_event(db, med)
            return f"✅ Refill recorded. <b>{_esc(med.name)}</b> remaining: {med.remaining_quantity:.0f} tablets"

        case QnaIntent():
            return await qa_service.answer_question(intent.question, chat_id=chat_id, history=history, placeholder_id=placeholder_id)

        case ConvesationIntent():
            return await conversation_respond(intent.message, chat_id=chat_id, history=history, placeholder_id=placeholder_id)

        case ListMedicationsIntent():
            meds = await medication_service.list_medications(db, user.id)
            if not meds:
                return "You have no medications added yet. Try: 'Take Crocin morning and night'"
            import pytz
            user_tz = pytz.timezone(settings.user_timezone)
            lines = ["📋 <b>Your Medications:</b>\n"]
            for m in meds:
                times_str = _esc(", ".join(m["times"])) if m["times"] else "no times set"
                remaining = f"{m['remaining_quantity']:.0f} tablets left"
                if m["next_reminder"]:
                    local_time = m["next_reminder"].astimezone(user_tz).strftime("%d %b %I:%M %p")
                    next_str = f"next reminder: {local_time}"
                else:
                    next_str = "no upcoming reminder"
                lines.append(f"• <b>{_esc(m['name'])}</b> — {times_str} | {remaining} | {next_str}")
            return "\n".join(lines)

        case UnknownIntent():
            return "I didn't understand that. Try: 'Take Crocin morning and night' or 'I have 30 Crocin tablets'."

        case AddToCartIntent():
            item = await cart_service.add_to_cart(db, user.id, intent.medicine_name, intent.quantity)
            if item is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b> in your medications."
            return f"🛒 Added <b>{_esc(intent.medicine_name)}</b> (×{item.quantity}) to your cart."

        case ViewCartIntent():
            items = await cart_service.get_cart_items(db, user.id)
            if not items:
                return "Your cart is empty. Add medicines with 'Add Crocin to cart'."
            comparison = await cart_service.format_cart_comparison(items)
            return comparison

        case RemoveFromCartIntent():
            removed = await cart_service.remove_from_cart(db, user.id, intent.medicine_name)
            if not removed:
                return f"<b>{_esc(intent.medicine_name)}</b> is not in your cart."
            return f"✅ Removed <b>{_esc(intent.medicine_name)}</b> from your cart."

        case CheckoutIntent():
            items = await cart_service.get_cart_items(db, user.id)
            if not items:
                return "Your cart is empty. Add medicines first with 'Add Crocin to cart'."
            comparison = await cart_service.format_cart_comparison(items)
            return comparison + "\n\nAfter ordering, reply: <i>'Ordered 2 strips Crocin'</i> to update your stock."

        case OrderConfirmedIntent():
            med = await cart_service.confirm_order(db, user.id, intent.medicine_name, intent.quantity)
            if med is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b> in your medications."
            await reminder_service.regenerate_refill_event(db, med)
            return f"✅ Order confirmed! <b>{_esc(med.name)}</b> stock updated. Remaining: {med.remaining_quantity:.0f} tablets."

        case StopMedicationIntent():
            med = await medication_service.stop_medication(db, user.id, intent.medicine_name)
            if med is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b> in your medications."
            return f"🛑 <b>{_esc(med.name)}</b> has been stopped. All pending reminders cancelled."

        case PauseMedicationIntent():
            from app.services.medication_service import get_medication
            med = await get_medication(db, user.id, intent.medicine_name)
            if med is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b> in your medications."
            if intent.days is None:
                # Ask user for duration
                await message_log_service.set_pending_action(db, user.id, {
                    "action": "pause_medication",
                    "medicine_name": intent.medicine_name
                })
                return f"⏸️ How long do you want to pause <b>{_esc(intent.medicine_name)}</b>? Reply with number of days (e.g. '3') or 'indefinitely'."
            med = await medication_service.pause_medication(db, user.id, intent.medicine_name, intent.days)
            return f"⏸️ <b>{_esc(med.name)}</b> paused for {intent.days} day(s). Reminders will resume after that."

        case ResumeMedicationIntent():
            med = await medication_service.resume_medication(db, user.id, intent.medicine_name)
            if med is None:
                return f"I couldn't find <b>{_esc(intent.medicine_name)}</b> in your medications."
            await reminder_service.regenerate_dose_events(db, med)
            await reminder_service.regenerate_refill_event(db, med)
            return f"▶️ <b>{_esc(med.name)}</b> resumed. Reminders are back on schedule."

        case AdherenceIntent():
            stats = await medication_service.get_adherence_stats(db, user.id)
            meds = stats.get("medications", [])
            if not meds:
                return "No medication data yet. Start taking your medications and I'll track your adherence."
            lines = ["📊 <b>Your Adherence Report:</b>\n"]
            for m in meds:
                if m["adherence_pct"] is not None:
                    bar = "🟢" if m["adherence_pct"] >= 80 else "🟡" if m["adherence_pct"] >= 50 else "🔴"
                    lines.append(f"{bar} <b>{_esc(m['name'])}</b> — {m['adherence_pct']}% ({m['taken']}/{m['total']} doses taken)")
                else:
                    lines.append(f"⚪ <b>{_esc(m['name'])}</b> — no data yet")
            return "\n".join(lines)

        case _:
            return "Something went wrong. Please try again."


async def _handle_photo(db, user, chat_id: str, photo: list) -> None:
    file_id = photo[-1].get("file_id") if isinstance(photo[-1], dict) else getattr(photo[-1], "file_id", None)
    if not file_id:
        await send_message(chat_id, "Could not process the image. Please try again.")
        return

    # Get file_path from Telegram
    async with __import__("httpx").AsyncClient() as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        if resp.status_code != 200:
            await send_message(chat_id, "Could not process the image. Please try again.")
            return
        file_path = resp.json().get("result", {}).get("file_path")

    if not file_path:
        await send_message(chat_id, "Could not process the image. Please try again.")
        return

    file_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"

    try:
        prescription = await download_and_store(db, user, file_url)
        medications = await extract_medications(db, prescription)
    except Exception:
        logger.exception("Prescription extraction failed")
        await send_message(chat_id, "Could not process the prescription. Please try again or add medications manually.")
        return

    if not medications:
        await send_message(chat_id, "No medications found in the image. Please add them manually.")
        return

    lines = ["Found these medications in your prescription. Reply 'confirm' to add them all:\n"]
    for m in medications:
        lines.append(f"• {_esc(m.get('medicine_name'))} — {_esc(', '.join(m.get('dosage_times', [])))}")
    await send_message(chat_id, "\n".join(lines))


async def _handle_voice(db, user, chat_id: str, voice) -> None:
    from app.config import settings

    # Check duration limit
    if voice.duration > settings.voice_max_duration_seconds:
        await send_message(chat_id, f"⚠️ Voice messages must be {settings.voice_max_duration_seconds} seconds or shorter.")
        return

    await send_typing(chat_id)

    # Download audio
    audio_bytes = await get_voice_bytes(voice.file_id)
    if not audio_bytes:
        await send_message(chat_id, "Could not download your voice message. Please try again.")
        return

    # Transcribe
    transcribed = await transcribe(audio_bytes)
    if not transcribed:
        await send_message(chat_id, "Could not understand your voice message. Please try again or type your message.")
        return

    logger.info("Voice transcription for user %s: %s", user.telegram_id, transcribed)

    # Log as regular message
    try:
        msg_record = await message_log_service.log_message(db, user.id, transcribed)
    except Exception:
        logger.exception("Failed to log voice message")
        msg_record = None

    # Parse and route through existing pipeline
    intents = await parse_intent(transcribed)

    if msg_record:
        try:
            await message_log_service.update_parsed_intent(db, msg_record, intents)
        except Exception:
            logger.exception("Failed to update parsed intent for voice message")

    replies = []
    for intent in intents:
        reply = await _route_intent(db, user, intent, chat_id)
        replies.append(reply)

    combined = "\n\n".join(r for r in replies if r)
    if combined:
        await send_message(chat_id, combined)

    if msg_record:
        try:
            await message_log_service.store_bot_reply(db, msg_record, combined)
        except Exception:
            logger.exception("Failed to store bot reply for voice message")


async def _handle_pending_action(db, user, pending: dict, user_reply: str) -> str:
    """Handle a follow-up response to a pending action."""
    action = pending.get("action")

    if action == "pause_medication":
        medicine_name = pending.get("medicine_name", "")
        text = user_reply.strip().lower()

        # Parse days from reply
        days = None
        if text in ("indefinitely", "indefinite", "forever", "no end"):
            days = None
        else:
            import re
            match = re.search(r"\d+", text)
            if match:
                days = int(match.group())
            else:
                return f"I didn't understand that. Please reply with a number of days (e.g. '3') or 'indefinitely'."

        med = await medication_service.pause_medication(db, user.id, medicine_name, days)
        if med is None:
            return f"I couldn't find <b>{_esc(medicine_name)}</b> in your medications."
        if days:
            return f"⏸️ <b>{_esc(med.name)}</b> paused for {days} day(s). Reminders will resume automatically."
        return f"⏸️ <b>{_esc(med.name)}</b> paused indefinitely. Say 'Resume {_esc(med.name)}' when you're ready."

    return "I'm not sure what you meant. Please try again."


async def _process_callback(callback: dict) -> None:
    """Handle inline button presses: taken / skipped / snooze / cart_add / buy_now."""
    try:
        await _process_callback_inner(callback)
    except Exception:
        logger.exception("Unhandled error in _process_callback")


DOSE_ACTIONS = ("taken", "skipped", "snooze")
CART_ACTIONS = ("cart_add", "buy_now")


async def _process_callback_inner(callback: dict) -> None:
    from app.services.notification_service import answer_callback_query

    callback_id = callback.get("id", "")
    data = callback.get("data", "") or ""
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    message_id = callback.get("message", {}).get("message_id")
    telegram_id = str(callback.get("from", {}).get("id", ""))

    if not data or not chat_id or not telegram_id:
        return

    action, _, target_id = data.partition(":")
    if not target_id:
        return
    try:
        target_uuid = uuid.UUID(target_id)
    except ValueError:
        logger.warning("Callback with non-UUID target: %s", action)
        return

    async with AsyncSessionLocal() as db:
        user = await get_or_create_user(db, telegram_id)

        if action in DOSE_ACTIONS:
            await _handle_dose_callback(
                db, user, action, target_uuid, callback_id, chat_id, message_id
            )
        elif action in CART_ACTIONS:
            await _handle_cart_callback(
                db, user, action, target_uuid, callback_id, chat_id, message_id
            )
        else:
            logger.warning("Unknown callback action: %s", action)


async def _handle_dose_callback(
    db, user, action: str, event_id, callback_id: str, chat_id: str, message_id: int | None
) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.exc import IntegrityError

    from app.models import DoseLog, Medication, ReminderEvent
    from app.services.medication_service import decrement_dose
    from app.services.notification_service import answer_callback_query, edit_message

    event = await db.get(ReminderEvent, event_id)
    # An event id is guessable in principle — never act on another user's event.
    if event is None or event.user_id != user.id:
        await answer_callback_query(callback_id, "Event not found.")
        return

    medication = await db.get(Medication, event.medication_id)
    med_name = esc(medication.name) if medication else "medication"

    if action == "snooze":
        event.trigger_time = datetime.now(timezone.utc) + timedelta(minutes=15)
        event.status = "pending"
        await db.commit()
        await answer_callback_query(callback_id, "⏰ Snoozed 15 minutes.")
        if message_id:
            await edit_message(chat_id, message_id, f"⏰ <b>{med_name}</b> — snoozed 15 min")
        return

    # taken / skipped — one log per reminder, so a repeated tap is a no-op.
    already_logged = await db.scalar(
        select(DoseLog).where(DoseLog.reminder_event_id == event.id)
    )
    if already_logged is not None:
        await answer_callback_query(callback_id, "Already recorded.")
        return

    db.add(DoseLog(
        user_id=user.id,
        medication_id=event.medication_id,
        reminder_event_id=event.id,
        status=action,
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race against a concurrent tap — the other one recorded it.
        await db.rollback()
        await answer_callback_query(callback_id, "Already recorded.")
        return

    if action == "taken":
        # Stock drops only once the user confirms they actually took the dose.
        await decrement_dose(db, event.medication_id)
        await answer_callback_query(callback_id, "✅ Marked as taken!")
        if message_id:
            await edit_message(chat_id, message_id, f"✅ <b>{med_name}</b> — taken")
    else:
        await answer_callback_query(callback_id, "❌ Marked as skipped.")
        if message_id:
            await edit_message(chat_id, message_id, f"❌ <b>{med_name}</b> — skipped")


async def _handle_cart_callback(
    db, user, action: str, medication_id, callback_id: str, chat_id: str, message_id: int | None
) -> None:
    from app.models import Medication
    from app.services.cart_service import add_to_cart, format_buy_links
    from app.services.notification_service import answer_callback_query, edit_message

    medication = await db.get(Medication, medication_id)
    if medication is None or medication.user_id != user.id:
        await answer_callback_query(callback_id, "Could not find medication.")
        return

    med_name = esc(medication.name)

    if action == "cart_add":
        await add_to_cart(db, user.id, medication.name)
        await answer_callback_query(callback_id, f"🛒 {medication.name} added to cart!")
        if message_id:
            await edit_message(
                chat_id, message_id,
                f"⚠️ <b>{med_name}</b> is running low.\n✅ Added to cart.",
            )
    else:  # buy_now
        await answer_callback_query(callback_id, "Fetching prices...")
        await send_message(chat_id, await format_buy_links(medication.name))
