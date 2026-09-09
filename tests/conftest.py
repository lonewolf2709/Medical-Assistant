"""Shared test fixtures. Runs against a real Postgres test database."""
import os

# Point the app at the test database and neutral credentials BEFORE app modules
# are imported — engine and API base URLs are built at import time.
_ADMIN = os.environ.get("TEST_DATABASE_URL")
if not _ADMIN:
    from dotenv import dotenv_values

    _ADMIN = dotenv_values(".env")["DATABASE_URL"].rsplit("/", 1)[0] + "/medibuddy_test"
os.environ["DATABASE_URL"] = _ADMIN
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
os.environ["GEMINI_API_KEY"] = "test-gemini-key"
os.environ["LOG_REQUESTS"] = "false"
os.environ["DEBUG"] = "false"
os.environ["USER_TIMEZONE"] = "Asia/Kolkata"

import pytest
import pytest_asyncio

from app.database import AsyncSessionLocal, Base, engine
from app.models import User

WEBHOOK_SECRET = "test-secret"


@pytest_asyncio.fixture(scope="function", autouse=True)
async def _schema():
    """Fresh schema per test — cheap at this table count and keeps tests isolated."""
    # The engine is module-level and pools connections bound to one event loop;
    # each test gets a new loop, so drop the pooled connections around every test.
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def user(db):
    u = User(telegram_id="42")
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@pytest.fixture
def sent(monkeypatch):
    """Capture all outbound Telegram calls instead of performing them."""
    calls = {"messages": [], "edits": [], "buttons": [], "callbacks": [], "deletes": []}

    async def send_message(chat_id, text):
        calls["messages"].append((chat_id, text))
        return True

    async def send_message_get_id(chat_id, text):
        calls["messages"].append((chat_id, text))
        return 1000 + len(calls["messages"])

    async def send_message_with_buttons(chat_id, text, buttons):
        calls["buttons"].append((chat_id, text, buttons))
        return 2000 + len(calls["buttons"])

    async def edit_message(chat_id, message_id, text):
        calls["edits"].append((chat_id, message_id, text))
        return True

    async def delete_message(chat_id, message_id):
        calls["deletes"].append((chat_id, message_id))
        return True

    async def answer_callback_query(callback_query_id, text=""):
        calls["callbacks"].append((callback_query_id, text))

    async def send_typing(chat_id):
        return None

    stubs = {
        "send_message": send_message,
        "send_message_get_id": send_message_get_id,
        "send_message_with_buttons": send_message_with_buttons,
        "edit_message": edit_message,
        "delete_message": delete_message,
        "answer_callback_query": answer_callback_query,
        "send_typing": send_typing,
    }
    # Patch at the definition site and at every module that imported by name.
    import app.routers.webhook as webhook_mod
    import app.scheduler as scheduler_mod
    import app.services.notification_service as notif

    for name, fn in stubs.items():
        monkeypatch.setattr(notif, name, fn, raising=False)
        for mod in (webhook_mod, scheduler_mod):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, fn, raising=False)
    return calls
