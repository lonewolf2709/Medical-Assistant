import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Message
from app.schemas import ParsedIntent

logger = logging.getLogger(__name__)


async def log_message(db: AsyncSession, user_id: uuid.UUID, raw_text: str) -> Message:
    msg = Message(user_id=user_id, raw_text=raw_text)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def update_parsed_intent(db: AsyncSession, message: Message, intents: list[ParsedIntent] | ParsedIntent) -> None:
    if isinstance(intents, list):
        message.parsed_intent = json.dumps([i.model_dump() for i in intents])
    else:
        message.parsed_intent = json.dumps(intents.model_dump())
    await db.commit()


async def store_bot_reply(db: AsyncSession, message: Message, reply: str) -> None:
    message.bot_reply = reply
    await db.commit()


async def get_recent_history(db: AsyncSession, user_id: uuid.UUID, limit: int = 10) -> list[dict]:
    """Return last N messages as [{role, content}] pairs for Gemini chat history."""
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id, Message.bot_reply.isnot(None))
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    messages = result.scalars().all()
    # Reverse to get chronological order
    history = []
    for msg in reversed(messages):
        history.append({"role": "user", "parts": [msg.raw_text]})
        history.append({"role": "model", "parts": [msg.bot_reply]})
    return history


async def get_pending_action(
    db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None
) -> dict | None:
    """Get the most recent unexpired pending action for a user, if any.

    Expiry matters: without it an abandoned follow-up prompt ("how many days?")
    silently swallows the user's next unrelated message hours later.
    """
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id, Message.pending_action.isnot(None))
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    msg = result.scalars().first()
    if not msg or not msg.pending_action:
        return None

    try:
        action = json.loads(msg.pending_action)
    except ValueError:
        logger.warning("Discarding unreadable pending_action on message %s", msg.id)
        msg.pending_action = None
        await db.commit()
        return None

    expires_at = action.get("expires_at")
    if expires_at and now > datetime.fromisoformat(expires_at):
        msg.pending_action = None
        await db.commit()
        return None
    return action


async def clear_pending_action(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Clear the most recent pending action for a user."""
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id, Message.pending_action.isnot(None))
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    msg = result.scalars().first()
    if msg:
        msg.pending_action = None
        await db.commit()


async def set_pending_action(db: AsyncSession, user_id: uuid.UUID, action: dict) -> None:
    """Store a pending action, stamped with an expiry, on the most recent message."""
    result = await db.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    msg = result.scalars().first()
    if msg is None:
        logger.warning("No message to attach a pending action to for user %s", user_id)
        return
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.pending_action_ttl_minutes
    )
    msg.pending_action = json.dumps({**action, "expires_at": expires_at.isoformat()})
    await db.commit()
