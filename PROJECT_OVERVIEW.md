# MediBuddy — Medication Assistant Bot

## What is it?

MediBuddy is a Telegram chatbot that helps users manage their medications through natural language. Users interact by typing or speaking, and the bot handles everything from adding medications and setting reminders to answering health questions and suggesting OTC remedies.

Built with Python FastAPI, PostgreSQL, Telegram Bot API, and Google Gemini 2.5 Flash.

---

## Features Built

### 1. Natural Language Medication Management
Users can add medications by typing naturally — no commands needed.
- "Take Crocin morning and night" → adds medication with reminders at 8 AM and 9 PM
- "I have ordered 5 strips of Paracetamol" → records a refill (1 strip = 10 tablets)
- "Change my Crocin reminder to 9 PM" → updates reminder times only
- "Show my medications" → lists all active medications with dosage times, tablets left, and next reminder

### 2. Upsert Semantics for Medications
Re-adding an existing medication (same name, same user) updates it rather than creating a duplicate. Name matching is case-insensitive — "crocin" and "Crocin" are treated as the same medication.

### 3. Automatic Reminder Scheduling
When a medication is added or updated, the bot automatically precomputes dose reminders for the next 7 days and stores them in the `reminder_events` table.
- Slot-based times: morning (8 AM IST), evening (6 PM IST), night (9 PM IST)
- Custom times: "17:30", "06:00", etc.
- All times stored in UTC internally, converted from user's local timezone (IST by default, configurable)
- Old pending events are deleted and regenerated on every update
- A daily Celery Beat task (`top_up_reminders`) rolls the window forward, so reminders keep
  arriving for users who never message the bot again. It is idempotent — existing trigger
  times are never duplicated.

### 4. Refill Alerts
The bot tracks remaining tablet count per medication. When remaining quantity divided by daily dose is within 5 days of running out, a refill `reminder_event` is precomputed and stored. The alert fires automatically via the scheduler.

If a medication is *already* inside the alert window (or out of stock) the alert is scheduled
immediately rather than skipped. The reminder carries "Add to Cart" and "Buy Now" buttons.

### 5. Dose Quantity Tracking
Stock is decremented when the user taps **✅ Taken** — not when the reminder is sent, since at
send time we do not yet know whether they will take or skip the dose. `remaining_quantity` drops
by `daily_dose / number_of_doses_per_day` and never goes below zero.

Each reminder can be logged only once: `dose_logs.reminder_event_id` is unique, so repeated taps
on the same reminder cannot double-count a dose or double-decrement stock.

### 6. Strip-to-Tablet Conversion
When recording a refill with unit "strip", the bot converts it to tablets automatically (1 strip = 10 tablets). Both `remaining_quantity` and `total_quantity` are updated.

### 7. Background Notification Scheduler
A polling task runs every 60 seconds in a **Celery worker driven by Celery Beat** — not inside the
FastAPI process. It:
- Queries `reminder_events` where `trigger_time <= NOW()` and `status = pending`
- Uses `SELECT FOR UPDATE SKIP LOCKED` so two workers never send the same reminder. The row locks
  are held until a single commit at the end of the batch; committing mid-loop would release them
  early and let a second worker re-send events still being processed
- Sends Telegram notifications with the (HTML-escaped) medication name
- Marks events as `sent` after successful delivery
- Leaves them `pending` if delivery fails, so the next cycle retries
- Auto-resumes a paused medication once `paused_until` has passed

Exactly one Beat process must run: a second one would send every reminder twice. In
`docker-compose.yml` Beat is its own service, so `celery-worker` can be scaled freely.

### 8. Multi-Intent Message Parsing
A single message can contain multiple intents. The parser returns a list and all intents are executed:
- "Add Crocin 30 tablets and set reminder to 4 PM" → `[ADD_MEDICATION, SET_REMINDER]`
- Both actions execute and the user gets a combined confirmation
- Spurious `UNKNOWN` intents are filtered out if valid intents are also present

### 9. Supported Intent Types
- `ADD_MEDICATION` — add or update a medication with dosage times and quantity
- `SET_REMINDER` — update reminder times for an existing medication
- `REFILL` — record a tablet/strip refill
- `QNA` — medicine questions and symptom descriptions
- `LIST_MEDICATIONS` — show all active medications
- `CONVERSATION` — greetings, small talk, general chat
- `UNKNOWN` — fallback for unrecognised input

### 10. Medicine Q&A with OTC Suggestions
Users can ask general medicine questions or describe symptoms. The bot responds with:
- Common OTC medication suggestions with brand names (Crocin, Dolo, Allegra, Brufen, etc.)
- Typical adult dosages where well-known
- Guardrails to decline clinical diagnosis, prescription-only drugs, or personalised treatment plans
- A medical disclaimer appended to every response
- Symptom statements ("I am feeling feverish", "my stomach hurts") are routed to QNA automatically

### 11. Conversational Interface
The bot handles greetings and small talk as MediBuddy — warm, concise, always steering toward medication management. Responds to "hi", "how are you", "thanks", etc.

### 12. Conversation Context / Memory
For Q&A and conversational messages, the bot loads the last 10 message exchanges (user text + bot reply) from the database and passes them as chat history to Gemini. This enables follow-up questions like "what about the dosage?" to work correctly in context.

### 13. Voice Message Support
Users can send voice messages (OGG Opus, up to 45 seconds). The bot:
- Rejects messages over 45 seconds with a clear error
- Downloads the OGG audio file from Telegram's file API (via `getFile` + download)
- Transcribes it using Gemini's native audio understanding (no separate STT service)
- Processes the transcription through the exact same intent pipeline as text
- Logs the transcribed text in the messages table

### 14. Prescription Image Upload
Users can photograph a prescription and send it. The bot:
- Downloads the image from Telegram
- Stores the image URL in the `prescriptions` table
- Runs OCR + Gemini to extract medication names and dosages
- Presents the extracted list to the user for confirmation before adding

### 15. Streaming Responses (Typewriter Effect)
For Q&A and conversational responses, the bot streams Gemini's output to Telegram using message edits:
- A `💭 Thinking...` placeholder appears immediately
- Text updates every ~0.8 seconds as Gemini streams chunks
- Final complete response replaces the placeholder
- For non-streamed intents (add medication, refill, etc.), the placeholder is edited with the final reply
- For mixed intent messages, each intent type is handled independently

### 16. Immediate Webhook Acknowledgement
The webhook returns `{"ok": True}` to Telegram instantly (before any processing). Processing runs in
a FastAPI `BackgroundTasks` job, which Starlette awaits after the response is sent — unlike a bare
`asyncio.create_task`, whose task holds no strong reference and can be garbage-collected mid-flight.
This prevents Telegram from retrying the same message due to timeout.

Work still lives in-process, so anything in flight is lost on restart. Dispatching to Celery (the
worker already exists) would make it durable.

### 17. Typing Indicator
A "typing..." indicator is sent to Telegram before processing begins, so the user sees the bot is active. Failures are silently swallowed — the indicator is best-effort and never crashes the request.

### 18. Auto User Registration
When a Telegram user sends their first message, a user record is created automatically with a UUID and their Telegram ID. Subsequent messages retrieve the existing record — no duplicate users.

### 19. Message Logging
All incoming messages are stored in the `messages` table with:
- Raw text
- All parsed intents (as a JSON array)
- Bot reply
- Timestamp
Logging failures never interrupt message processing.

### 20. Dev Testing Endpoints (DEBUG only)
A `/dev` router provides HTTP endpoints for testing without Telegram. These accept an arbitrary
`telegram_id` and can therefore act as any user, so they are mounted **only when `DEBUG=true`**:
- `POST /dev/parse` — parse raw text into intents
- `POST /dev/chat` — full pipeline (parse + route) without Telegram
- `POST /dev/qa` — test Q&A directly
- `POST /dev/medication/add` — add a medication via HTTP
- `POST /dev/medication/refill` — record a refill via HTTP

### 21. Health Check
`GET /health` returns `{"status": "ok"}` — used for uptime monitoring.

---

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python FastAPI |
| Database | PostgreSQL (async via asyncpg) |
| ORM | SQLAlchemy async |
| Migrations | Alembic |
| LLM | Google Gemini 2.5 Flash |
| Bot | Telegram Bot API |
| HTTP client | httpx |
| Task queue / scheduler | Celery + Celery Beat (Redis broker) |
| Tests | pytest + pytest-asyncio (against a real Postgres) |
| Timezone | pytz |

---

## Architecture

```
Telegram User
    ↓ sends message / voice / photo
Telegram Servers
    ↓ POST /webhook
FastAPI  →  verifies X-Telegram-Bot-Api-Secret-Token, returns 200 immediately
    ↓ BackgroundTasks
Intent Parser (Gemini)
    ↓ list of structured intents
Router → Services (per intent)
    ├── medication_service → PostgreSQL
    ├── reminder_service → PostgreSQL
    ├── qa_service → Gemini (streaming via message edits)
    ├── conversation_service → Gemini (streaming via message edits)
    ├── voice_service → Telegram file API → Gemini transcription
    └── prescription_service → OCR → Gemini
Notification Service → Telegram sendMessage / editMessage / deleteMessage

Celery Beat
    ├── poll_reminders (every 60s)
    │      → queries reminder_events (FOR UPDATE SKIP LOCKED)
    │      → sends dose/refill notifications with inline buttons
    │      → marks them sent (stock changes only when the user taps "Taken")
    └── top_up_reminders (daily)
           → extends each active medication's dose events over the next 7 days
```

---

## Database Schema

| Table | Purpose |
|---|---|
| users | Telegram user registry (UUID + telegram_id) |
| medications | Medication records per user (name, quantity, daily_dose) |
| dosage_times | Slot or custom time per medication |
| reminder_events | Precomputed future notifications (dose + refill) |
| prescriptions | Uploaded prescription image URLs + extracted text |
| messages | Full message log (raw text, parsed intents JSON, bot reply, pending action) |
| dose_logs | One row per answered reminder — taken or skipped (unique per reminder) |
| cart_items | Reorder basket per user (active / ordered / removed) |

---

## Configuration (`.env`)

| Key | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL async connection string |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | **Required.** Shared secret Telegram sends back in `X-Telegram-Bot-Api-Secret-Token`; `/webhook` rejects every request without it |
| `REDIS_URL` | Celery broker / result backend |
| `GEMINI_API_KEY` | Google AI API key |
| `GEMINI_MODEL` | Model name (default: `gemini-2.5-flash-preview-04-17`) |
| `USER_TIMEZONE` | User's local timezone (default: `Asia/Kolkata`) |
| `SCHEDULER_INTERVAL_SECONDS` | How often scheduler polls (default: 60) |
| `REMINDER_PRECOMPUTE_DAYS` | Days ahead to precompute reminders (default: 7) |
| `REFILL_ALERT_DAYS_BEFORE` | Days before depletion to trigger refill alert (default: 5) |
| `VOICE_MAX_DURATION_SECONDS` | Max voice message length (default: 45) |
| `PENDING_ACTION_TTL_MINUTES` | How long an unanswered follow-up prompt stays live (default: 30) |
| `DEBUG` | Mounts the `/dev` router; verbose logging (default: false) |
| `LOG_REQUESTS` | Logs full request bodies — contains health data, development only (default: false) |

---

## What's Not Built Yet (Potential Next Steps)

- **User timezone selection** — currently hardcoded to IST via config, no per-user setting
- **Medication deletion / deactivation** — no way to remove a medication yet
- **Dose confirmation** — user confirming they actually took a dose
- **Multi-language support** — currently English only
- **WhatsApp integration** — planned but out of scope initially
- **Web dashboard** — no admin or user-facing web UI
- **Production deployment** — currently local dev only (ngrok tunnel)
- **Celery migration** — scheduler is in-process; Celery needed for horizontal scale
- **Automated test suite** — no unit or integration tests yet
- **Rate limiting** — no protection against message flooding
- **Prescription confirmation flow** — extracted medications shown but "confirm" command not yet wired to add them

---

## Running Locally

```bash
cd medication-assistant-bot
source venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Register Telegram webhook (after starting ngrok):
```bash
ngrok http 8000
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://YOUR-NGROK-URL/webhook"
```


---

## Sprint 1 — Update / Delete / Pause Medication

### 22. Stop Medication
"Stop Paracetamol" / "Remove Crocin" / "Delete Dolo" → permanently stops the medication, cancels all pending reminders. Status set to `stopped` in DB.

### 23. Pause Medication
"Pause Crocin for 3 days" → pauses with auto-resume after N days.
"Pause Vitamin D" → asks user how long, stores pending action, resumes on next reply.
Scheduler skips paused/stopped medications. Auto-resumes when `paused_until` expires.

### 24. Resume Medication
"Resume Crocin" / "Start taking Vitamin D again" → reactivates medication, regenerates dose and refill reminders.

---

## Sprint 2 — Prescription Image Parsing (Complete)

### 25. Prescription Image Upload (Gemini Vision)
User sends a photo of a prescription → bot downloads image → sends to Gemini vision API → extracts medication names, dosage times, quantities as structured JSON → presents list to user for confirmation.

### 26. Prescription Confirmation Flow
User replies "confirm" / "yes" / "add them" → all extracted medications are added with reminders automatically. Extracted data stored in `prescriptions.extracted_text` as JSON.

---

## Sprint 3 — Dose Confirmation + Adherence Tracking

### 27. Dose Reminder Inline Buttons
Dose notifications now include three inline keyboard buttons:
- ✅ Taken → logs dose as taken, decrements remaining quantity
- ❌ Skipped → logs dose as skipped, quantity unchanged
- ⏰ Snooze 15min → reschedules reminder event by 15 minutes

### 28. Dose Logging
Every button tap creates a `dose_logs` record with `taken` or `skipped` status, linked to the user, medication, and reminder event.

### 29. Adherence Tracking
"How am I doing?" / "Show my adherence" → calculates adherence % per medication from dose logs. Displays color-coded report:
- 🟢 ≥80% adherence
- 🟡 50-79%
- 🔴 <50%

### 30. Dose Decrement on Confirmation Only
Quantity is decremented only when user taps ✅ Taken — not when the notification is sent. Skipped doses do not reduce stock.

---

## Sprint 4 — Medication Cart & Ordering (Phase 1 + 2)

### 31. Cart System
`cart_items` table tracks medicines the user wants to order. Status: `active` / `ordered` / `removed`.

### 32. Add to Cart
"Add Crocin to cart" / "Put Paracetamol in cart" / "I need to order Vitamin D" → inserts into `cart_items`. If already in cart, increments quantity.

### 33. Refill Notification Cart Buttons
When a refill reminder fires, the notification now includes two inline buttons:
- 🛒 Add to Cart → adds medication to cart, updates notification message
- 🛍️ Buy Now → fetches prices from Gemini and shows sorted buy links

### 34. View Cart with Price Comparison
"Show my cart" / "What do I need to order?" → fetches Gemini-estimated prices for all cart items across 1mg, PharmEasy, Apollo. Shows:
- Per-medicine prices on each platform
- Total sticker price per platform
- Platform-wide offers (e.g. "18% off above ₹500")
- Updated total after offers
- Best platform recommendation with buy links

### 35. Checkout
"Checkout" / "Order these items" → same as View Cart but appends instruction to confirm order after buying.

### 36. Remove from Cart
"Remove Crocin from cart" → marks cart item as `removed`.

### 37. Order Confirmation
"Ordered 2 strips Crocin" / "I bought Paracetamol" → updates `remaining_quantity` (+quantity × 10 tablets), marks cart item as `ordered`, regenerates refill reminder.

### 38. Single Medicine Buy Links (Buy Now)
For individual medicine price check → Gemini estimates prices on 1mg, PharmEasy, Apollo. Results sorted cheapest first with 🏆 tag on best price. Includes direct search links. Note: prices are approximate.

---

## Updated Database Schema

| Table | Purpose |
|---|---|
| users | Telegram user registry |
| medications | Medication records (now includes `status`, `paused_until`) |
| dosage_times | Slot or custom time per medication |
| reminder_events | Precomputed future notifications |
| prescriptions | Uploaded prescription images + extracted JSON |
| messages | Message log (raw text, parsed intents, bot reply, pending_action) |
| dose_logs | Taken/skipped log per reminder event |
| cart_items | Virtual cart for ordering (active/ordered/removed) |

---

## Updated Intent List

| Intent | Example |
|---|---|
| ADD_MEDICATION | "Take Crocin morning and night" |
| SET_REMINDER | "Change Crocin reminder to 9 PM" |
| REFILL | "I have 30 Crocin tablets" |
| STOP_MEDICATION | "Stop Paracetamol" |
| PAUSE_MEDICATION | "Pause Dolo for 3 days" |
| RESUME_MEDICATION | "Resume Vitamin D" |
| LIST_MEDICATIONS | "Show my medications" |
| ADHERENCE | "How am I doing?" |
| ADD_TO_CART | "Add Crocin to cart" |
| VIEW_CART | "Show my cart" |
| REMOVE_FROM_CART | "Remove Paracetamol from cart" |
| CHECKOUT | "Checkout" |
| ORDER_CONFIRMED | "Ordered 2 strips Crocin" |
| QNA | "What are side effects of ibuprofen?" / "I have a fever" |
| CONVERSATION | "Hi", "Thanks" |
| UNKNOWN | Unrecognised input |


---

## Security

- **Webhook authentication.** Telegram is configured with a `secret_token`, which it returns in the
  `X-Telegram-Bot-Api-Secret-Token` header on every call. `/webhook` compares it in constant time
  and rejects mismatches with 403. It fails closed: with no secret configured, every request is
  rejected, because an unauthenticated webhook lets anyone read or write any user's medication data
  by forging a `from.id`.

  Register it with:
  ```bash
  curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
    -d "url=https://<host>/webhook" \
    -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
  ```
- **Callback ownership.** Button presses carry a UUID in `callback_data`. Every handler checks that
  the reminder event or medication actually belongs to the user who pressed the button.
- **Output escaping.** All replies use `parse_mode=HTML`, so medication names and model output are
  escaped (`app/telegram_format.py`). An unescaped `<` makes Telegram reject the whole message with
  a 400, and the user would silently receive nothing.
- **`/dev` endpoints** are mounted only when `DEBUG=true`.
- **`LOG_REQUESTS`** defaults to off; request bodies contain health information.

---

## Adherence

`adherence_pct = doses taken / dose reminders actually sent`.

The denominator is reminders **sent**, so an ignored reminder counts as missed. Counting only
answered reminders would report 100% for someone who tapped "Taken" once and ignored twenty
reminders. Reminders that have not fired yet are excluded.

---

## Running the tests

```bash
pip install -r requirements-dev.txt
createdb medibuddy_test          # or: TEST_DATABASE_URL=postgresql+asyncpg://.../other_db
pytest
```

Tests run against a real PostgreSQL database (the schema is recreated per test), so
`FOR UPDATE SKIP LOCKED`, Postgres enums and UUID behaviour are exercised as in production. All
outbound Telegram and Gemini calls are stubbed — the suite makes no network requests.

---

## Known gaps

- **Single timezone.** `USER_TIMEZONE` is process-wide; `users` has no timezone column, so all
  users are reminded on one clock.
- **Message processing is in-process.** Durable across restarts only once dispatch moves to Celery.
- **Cart prices are model-generated.** `cart_service` asks Gemini for current INR prices, which are
  approximations from training data, not live prices. Treat "cheapest platform" as a hint.
