# MediBuddy — Medication Assistant Bot

A Telegram bot that manages medications through ordinary conversation. Users type or speak
naturally — "Take Crocin morning and night", "I have 30 tablets left", "how am I doing?" — and the
bot sets reminders, tracks stock, logs adherence, and warns before they run out.

Built with FastAPI, PostgreSQL, Celery and Google Gemini.

> **Not medical advice.** This is a personal project. It suggests common over-the-counter options
> and declines clinical diagnosis or prescription-only recommendations, but nothing here is a
> substitute for a healthcare professional.

---

## What it does

**Medication management** — Add, update, pause, resume and stop medications in natural language.
Re-adding an existing medication updates it rather than duplicating it, matched case-insensitively.

**Reminders** — Dose times are either slots (morning 8am, evening 6pm, night 9pm) or exact times
("17:30"). Reminder events are precomputed for the next 7 days, stored in UTC, and dispatched by a
Celery worker polling every 60 seconds. A daily task rolls the window forward so reminders keep
arriving indefinitely.

**Dose tracking** — Every reminder carries ✅ Taken / ❌ Skipped / ⏰ Snooze buttons. Stock is
decremented only when the user confirms they took the dose, and each reminder can be answered once.

**Adherence reporting** — "How am I doing?" returns a per-medication percentage, where the
denominator is reminders *sent*, so an ignored reminder counts as missed.

**Refill alerts** — The bot projects remaining tablets against the daily dose and warns before
depletion (default 5 days ahead, or immediately if already low), with Add to Cart / Buy Now buttons.

**Reorder cart** — Add medicines to a cart and get a price comparison across 1mg, PharmEasy and
Apollo with buy links. *Prices are model-generated approximations, not live data.*

**Medicine Q&A** — Answers medication questions and symptom descriptions with OTC suggestions and
typical adult dosages, streamed to Telegram as a typewriter effect via message edits.

**Voice messages** — Transcribed with Gemini's native audio understanding (no separate STT
service) and routed through the same intent pipeline as text.

**Prescription photos** — Extracted with Gemini vision into a medication list the user confirms
before anything is added.

**Multi-intent parsing** — One message can carry several intents: "Add Crocin 30 tablets and set
reminder to 4pm" becomes `ADD_MEDICATION` + `SET_REMINDER`, and both execute.

---

## Architecture

```
Telegram
   │  POST /webhook  (X-Telegram-Bot-Api-Secret-Token)
   ▼
FastAPI ── verifies secret, returns 200 immediately, processes in the background
   │
   ├── parser (Gemini)          → list of typed intents
   └── router → services
         ├── medication_service ──┐
         ├── reminder_service  ────┼──▶ PostgreSQL
         ├── cart_service      ────┘
         ├── qa_service / conversation_service → Gemini (streamed via message edits)
         ├── voice_service        → Telegram file API → Gemini transcription
         └── prescription_service → Gemini vision
                                        │
Celery Beat                             ▼
   ├── poll_reminders   (every 60s) → due reminder_events → Telegram
   └── top_up_reminders (daily)     → extends the precomputed window
```

Reminder dispatch uses `SELECT … FOR UPDATE SKIP LOCKED` with a single commit per batch, so
multiple workers never send the same reminder twice.

| Component | Technology |
|---|---|
| API | FastAPI (Python 3.11) |
| Database | PostgreSQL, async via asyncpg |
| ORM / migrations | SQLAlchemy 2 (async) + Alembic |
| Task queue | Celery + Celery Beat, Redis broker |
| LLM | Google Gemini 2.5 Flash (text, vision, audio) |
| Messaging | Telegram Bot API via httpx |
| Tests | pytest + pytest-asyncio |

---

## Getting started

### Prerequisites

- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Google AI (Gemini) API key
- Docker, or Python 3.11 + PostgreSQL 16 + Redis 7 locally

### 1. Configure

```bash
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and generate a webhook secret:

```bash
openssl rand -hex 32   # → TELEGRAM_WEBHOOK_SECRET
```

`TELEGRAM_WEBHOOK_SECRET` is required. `/webhook` fails closed without it — an unauthenticated
webhook would let anyone read or write any user's medication data by forging a Telegram user id.

### 2. Run with Docker

```bash
docker compose up --build
```

This runs migrations to completion, then starts the API on `:8000`, a Celery worker, and a single
Beat process. Scale workers freely (`--scale celery-worker=3`); never run more than one Beat, or
every reminder is sent twice.

### 3. Or run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt

createdb medibuddy
alembic upgrade head

uvicorn app.main:app --reload                              # API
celery -A app.celery_app worker --loglevel=info -Q celery  # worker
celery -A app.celery_app beat --loglevel=info              # scheduler
```

### 4. Point Telegram at it

Telegram needs a public HTTPS URL — use [ngrok](https://ngrok.com) for local development:

```bash
ngrok http 8000

curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://<your-host>/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Then message your bot. `GET /health` returns `{"status":"ok"}` for uptime checks.

---

## Tests

```bash
createdb medibuddy_test        # or set TEST_DATABASE_URL
pytest
```

48 tests run against a real PostgreSQL database (the schema is rebuilt per test) so
`FOR UPDATE SKIP LOCKED`, Postgres enums and UUID handling behave as in production. Every
outbound Telegram and Gemini call is stubbed — the suite makes no network requests.

---

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Async Postgres connection string | — |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | — |
| `TELEGRAM_WEBHOOK_SECRET` | **Required.** Shared secret Telegram echoes back on every call | — |
| `GEMINI_API_KEY` | Google AI API key | — |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.5-flash` |
| `REDIS_URL` | Celery broker / backend | `redis://localhost:6379/1` |
| `USER_TIMEZONE` | Timezone for slot times | `Asia/Kolkata` |
| `SCHEDULER_INTERVAL_SECONDS` | Reminder poll interval | `60` |
| `REMINDER_PRECOMPUTE_DAYS` | Days of reminders to keep queued | `7` |
| `REFILL_ALERT_DAYS_BEFORE` | Days before depletion to warn | `5` |
| `PENDING_ACTION_TTL_MINUTES` | Lifetime of an unanswered follow-up prompt | `30` |
| `VOICE_MAX_DURATION_SECONDS` | Max voice message length | `45` |
| `DEBUG` | Mounts the `/dev` router; verbose logs | `false` |
| `LOG_REQUESTS` | Logs full request bodies — contains health data | `false` |

---

## Project layout

```
app/
├── main.py                 create_app() factory, exception handlers, lifespan
├── config.py               pydantic-settings configuration
├── models.py               SQLAlchemy models and constraints
├── schemas.py              intent + Telegram payload schemas
├── celery_app.py           Celery config and Beat schedule
├── tasks.py                poll_reminders, top_up_reminders
├── scheduler.py            reminder dispatch (FOR UPDATE SKIP LOCKED)
├── telegram_format.py      HTML escaping for Telegram messages
├── routers/
│   ├── webhook.py          webhook auth, intent routing, button callbacks
│   ├── health.py           /health
│   └── dev.py              test endpoints, mounted only when DEBUG=true
└── services/
    ├── parser.py               natural language → typed intents (Gemini)
    ├── medication_service.py   CRUD, stock, adherence
    ├── reminder_service.py     event precomputation + daily top-up
    ├── notification_service.py Telegram delivery, pooled client, retry policy
    ├── cart_service.py         cart + price comparison
    ├── qa_service.py           medicine Q&A (streamed)
    ├── conversation_service.py small talk (streamed)
    ├── voice_service.py        audio download + transcription
    ├── prescription_service.py image → medication list
    ├── message_log_service.py  message log, history, pending actions
    └── user_service.py         get-or-create user
```

`PROJECT_OVERVIEW.md` documents every feature and design decision in detail.
`FLOW_REFERENCE.md` maps each message type to the exact functions that handle it, in order.

---

## Security notes

- **Webhook authentication** — the secret token is compared in constant time and fails closed.
- **Callback ownership** — button presses are checked against the presser, so one user cannot log
  a dose or add to a cart for another.
- **Output escaping** — all replies use `parse_mode=HTML`; medication names and model output are
  escaped, since an unescaped `<` makes Telegram reject the entire message.
- **`/dev` endpoints** accept an arbitrary user id, so they are mounted only when `DEBUG=true`.
- **`LOG_REQUESTS`** is off by default — request bodies contain health information.
- `.env` is gitignored; keep tokens out of the repository.

---

## Known gaps

- **One timezone for everyone.** `USER_TIMEZONE` is process-wide; `users` has no timezone column.
- **Message processing is in-process.** It survives the response but not a restart; moving webhook
  dispatch onto the existing Celery worker would make it durable.
- **Cart prices are model-generated**, not scraped or fetched from an API. Treat the "cheapest
  platform" verdict as a hint.
