# MediBuddy — Function Flow Reference

This document maps every message type and intent to the exact functions that process it, in order.

---

## Entry Points

There are two entry points into the system:

1. `POST /webhook` — Telegram sends all updates here (authenticated by secret token)
2. Celery Beat — `poll_reminders` every 60 seconds, `top_up_reminders` daily

---

## 1. Telegram Webhook Entry (`POST /webhook`)

```
Telegram → POST /webhook  (header: X-Telegram-Bot-Api-Secret-Token)
    → telegram_webhook(update: dict)          [webhook.py]
        → _secret_is_valid(header)            → 403 if missing/wrong (fails closed)
        ├── if callback_query → background.add_task(_process_callback)
        └── if message → background.add_task(_process_message)
    → returns {"ok": True} immediately
```

---

## 2. Regular Text Message Flow

```
_process_message(update)                      [webhook.py]
    → get_or_create_user(db, telegram_id)     [user_service.py]
    → check pending_action (pause follow-up)  [message_log_service.py → get_pending_action]
        └── if pending → _handle_pending_action(db, user, pending, raw_text)
    → check "confirm" keyword (prescription)
        └── if confirm → get_pending_prescription(db, user_id)  [prescription_service.py]
            → add_or_update_medication (×N)   [medication_service.py]
            → regenerate_dose_events          [reminder_service.py]
            → regenerate_refill_event         [reminder_service.py]
    → send_typing(chat_id)                    [notification_service.py]
    → send_message_get_id("💭 Thinking...")   [notification_service.py]
    → log_message(db, user_id, raw_text)      [message_log_service.py]
    → parse_intent(raw_text)                  [parser.py → Gemini API]
    → update_parsed_intent(db, msg, intents)  [message_log_service.py]
    → for each intent → _route_intent(db, user, intent, chat_id, placeholder_id)
    → edit_message / send_message (reply)     [notification_service.py]
    → store_bot_reply(db, msg, reply)         [message_log_service.py]
```

---

## 3. Intent Routing (`_route_intent`)

Each intent type maps to specific service calls:

### ADD_MEDICATION
```
_route_intent → AddMedicationIntent
    → add_or_update_medication(db, user_id, name, dosage_times, qty)  [medication_service.py]
        → get_medication (ilike lookup)
        → INSERT or UPDATE medications row
        → DELETE old dosage_times
        → _attach_dosage_times (INSERT dosage_times rows)
    → regenerate_dose_events(db, med)         [reminder_service.py]
        → DELETE pending dose reminder_events
        → INSERT new reminder_events (next 7 days, UTC-converted)
    → regenerate_refill_event(db, med)        [reminder_service.py]
        → DELETE pending refill reminder_events
        → INSERT refill reminder_event (if qty > 5-day threshold)
    → edit_message(placeholder, "✅ Added...")
```

### SET_REMINDER
```
_route_intent → SetReminderIntent
    → get_medication(db, user_id, name)       [medication_service.py]
    → add_or_update_medication (updates times only)
    → regenerate_dose_events(db, med)         [reminder_service.py]
    → edit_message(placeholder, "✅ Updated...")
```

### REFILL
```
_route_intent → RefillIntent
    → apply_refill(db, user_id, name, qty, unit)  [medication_service.py]
        → get_medication
        → qty × 10 if unit == "strip"
        → UPDATE remaining_quantity + total_quantity
    → regenerate_refill_event(db, med)        [reminder_service.py]
    → edit_message(placeholder, "✅ Refill recorded...")
```

### STOP_MEDICATION
```
_route_intent → StopMedicationIntent
    → stop_medication(db, user_id, name)      [medication_service.py]
        → get_medication
        → SET status = "stopped"
        → DELETE all pending reminder_events
    → edit_message(placeholder, "🛑 Stopped...")
```

### PAUSE_MEDICATION
```
_route_intent → PauseMedicationIntent
    ├── if days provided:
    │   → pause_medication(db, user_id, name, days)  [medication_service.py]
    │       → SET status = "paused", paused_until = now + days
    │       → DELETE all pending reminder_events
    │   → edit_message(placeholder, "⏸️ Paused...")
    └── if no days:
        → set_pending_action(db, user_id, {action: "pause_medication", medicine_name})
        → edit_message(placeholder, "How long to pause?")
        → next message → _handle_pending_action → pause_medication
```

### RESUME_MEDICATION
```
_route_intent → ResumeMedicationIntent
    → resume_medication(db, user_id, name)    [medication_service.py]
        → SET status = "active", paused_until = NULL
    → regenerate_dose_events(db, med)         [reminder_service.py]
    → regenerate_refill_event(db, med)        [reminder_service.py]
    → edit_message(placeholder, "▶️ Resumed...")
```

### LIST_MEDICATIONS
```
_route_intent → ListMedicationsIntent
    → list_medications(db, user_id)           [medication_service.py]
        → SELECT medications WHERE user_id
        → for each: SELECT dosage_times, next pending reminder_event
    → edit_message(placeholder, "📋 Your Medications...")
```

### ADHERENCE
```
_route_intent → AdherenceIntent
    → get_adherence_stats(db, user_id)        [medication_service.py]
        → SELECT medications WHERE active
        → for each: SELECT dose_logs (taken/skipped counts)
        → calculate adherence %
    → edit_message(placeholder, "📊 Adherence Report...")
```

### QNA
```
_route_intent → QnaIntent
    → get_recent_history(db, user_id)         [message_log_service.py]
    → answer_question(question, chat_id, history, placeholder_id)  [qa_service.py]
        → model.start_chat(history)
        → stream chunks from Gemini
        → edit_message every 0.8s (typewriter)
        → final edit_message with full response + disclaimer
```

### CONVERSATION
```
_route_intent → ConvesationIntent
    → get_recent_history(db, user_id)         [message_log_service.py]
    → respond(message, chat_id, history, placeholder_id)  [conversation_service.py]
        → model.start_chat(history)
        → stream chunks from Gemini
        → edit_message every 0.8s (typewriter)
        → final edit_message
```

### UNKNOWN
```
_route_intent → UnknownIntent
    → edit_message(placeholder, "I didn't understand that...")
```

---

## 4. Voice Message Flow

```
_process_message → message.voice detected
    → _handle_voice(db, user, chat_id, voice)  [webhook.py]
        → check voice.duration <= 45s
        → get_voice_bytes(file_id)             [voice_service.py]
            → GET /getFile (Telegram API)
            → GET audio file bytes
        → transcribe(audio_bytes)              [voice_service.py]
            → Gemini audio understanding (base64 OGG)
            → returns transcribed text
        → log_message(db, user_id, transcribed)
        → parse_intent(transcribed)            [parser.py]
        → for each intent → _route_intent(...)
        → send_message(chat_id, reply)
```

---

## 5. Photo / Prescription Flow

```
_process_message → message.photo detected
    → _handle_photo(db, user, chat_id, photo)  [webhook.py]
        → GET /getFile (Telegram API) → file_path
        → download_and_store(db, user, file_url)  [prescription_service.py]
            → INSERT prescriptions row
        → extract_medications(db, prescription)   [prescription_service.py]
            → download image bytes
            → Gemini vision: extract medications JSON
            → UPDATE prescriptions.extracted_text (JSON array)
        → send_message("Found X medications. Reply 'confirm'...")

--- user replies "confirm" ---

_process_message → raw_text == "confirm"
    → get_pending_prescription(db, user_id)    [prescription_service.py]
        → SELECT latest prescription with extracted_text
        → parse JSON array
    → for each medication:
        → add_or_update_medication             [medication_service.py]
        → regenerate_dose_events               [reminder_service.py]
        → regenerate_refill_event              [reminder_service.py]
    → send_message("✅ Added N medications...")
```

---

## 6. Inline Button Callback Flow (Dose Confirmation)

```
Telegram → POST /webhook (callback_query)
    → telegram_webhook → background.add_task(_process_callback)
    → _process_callback_inner(callback)        [webhook.py]
        → parse "action:uuid"; discard non-UUID targets
        → get_or_create_user(db, telegram_id)

        ├── taken / skipped / snooze → _handle_dose_callback
        │   → SELECT ReminderEvent by id
        │   → reject if event.user_id != user.id        (ownership check)
        │   ├── "snooze":
        │   │   → trigger_time = now + 15min, status = "pending"
        │   └── "taken" / "skipped":
        │       → if a dose_log already exists for this event → "Already recorded." and stop
        │       → INSERT dose_logs (unique on reminder_event_id; IntegrityError → duplicate tap)
        │       → if "taken": decrement_dose(db, medication_id)  [medication_service.py]
        │           → remaining_quantity -= daily_dose / dose_count, floored at 0
        │
        └── cart_add / buy_now → _handle_cart_callback
            → SELECT Medication by id
            → reject if medication.user_id != user.id  (ownership check)
            ├── "cart_add" → add_to_cart(db, user.id, med.name)  [cart_service.py]
            └── "buy_now"  → send_message(format_buy_links(med.name))
```

---

## 7. Background Scheduler Flow

```
Celery Beat → poll_reminders                  [tasks.py]
    → _run(_process_due_events)                (fresh engine + event loop per run)
        → _process_due_events(db)              [scheduler.py]
            → SELECT reminder_events WHERE trigger_time <= NOW AND status = "pending"
              FOR UPDATE SKIP LOCKED           (locks held until the single commit below)
            → FOR EACH event:
                → SELECT User
                → SELECT Medication
                → check medication.status:
                    ├── "paused" + paused_until expired → SET status = "active"
                    ├── "paused" / "stopped" → mark event "sent", skip
                    └── "active" → proceed

                ├── event.type == "dose":
                │   → send_message_with_buttons(telegram_id, text, [Taken/Skipped/Snooze])
                │   → if success: SET event.status = "sent"
                │     (stock is NOT changed here — only when the user taps "Taken")

                └── event.type == "refill":
                    → send_message_with_buttons(telegram_id, warning, [Add to Cart/Buy Now])
                    → if success: SET event.status = "sent"

            → single db.commit() after the whole batch
```

### Daily top-up

```
Celery Beat → top_up_reminders                [tasks.py]
    → top_up_dose_events(db)                  [reminder_service.py]
        → SELECT medications WHERE status = "active"
        → FOR EACH: SELECT dosage_times, SELECT existing dose trigger_times
            → for each of the next REMINDER_PRECOMPUTE_DAYS days × dosage time:
                → skip if in the past or already present  (idempotent)
                → INSERT reminder_events (type="dose", status="pending")
```

---

## 8. Notification Service Functions

| Function | Purpose |
|---|---|
| `send_message(chat_id, text)` | Send plain text message |
| `send_message_get_id(chat_id, text)` | Send message, return message_id for editing |
| `edit_message(chat_id, message_id, text)` | Edit existing message (streaming/placeholder) |
| `delete_message(chat_id, message_id)` | Delete a message |
| `send_typing(chat_id)` | Show "typing..." indicator |
| `send_message_with_buttons(chat_id, text, buttons)` | Send message with inline keyboard |
| `answer_callback_query(callback_id, text)` | Acknowledge button press |

---

## 9. Database Write Points Summary

| Operation | Table(s) Written |
|---|---|
| First message | users |
| Add medication | medications, dosage_times, reminder_events |
| Set reminder | dosage_times (delete+insert), reminder_events (delete+insert) |
| Refill | medications (qty update), reminder_events (refill update) |
| Stop medication | medications (status), reminder_events (delete pending) |
| Pause medication | medications (status, paused_until), reminder_events (delete pending) |
| Resume medication | medications (status, paused_until=NULL), reminder_events (regenerated) |
| Prescription upload | prescriptions |
| Prescription confirm | medications, dosage_times, reminder_events |
| Dose taken/skipped | dose_logs |
| Snooze | reminder_events (trigger_time update) |
| Scheduler fires | reminder_events (status=sent), medications (remaining_quantity) |
| Any message | messages (raw_text, parsed_intent, bot_reply) |
| Pause follow-up | messages (pending_action) |
