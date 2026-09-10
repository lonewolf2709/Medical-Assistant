"""Conversational responses with a streaming typewriter effect."""
import logging
import time

from app.services import llm
from app.services.notification_service import edit_message, send_message_get_id

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTION = """You are MediBuddy, a friendly medication assistant bot on Telegram.

Your personality:
- Warm, caring, and supportive
- Concise — keep replies short (2-4 sentences max)
- Always steer the conversation toward helping with medications

When someone greets you or makes small talk:
- Greet them back warmly
- Ask how they're feeling / how their health is going
- Remind them what you can help with:
  • Adding medications and setting reminders
  • Tracking remaining tablets
  • Refill alerts
  • Answering general medicine questions
  • Viewing their current medication list

Never provide medical diagnoses or treatment advice.
Always end with a gentle prompt to help them with their medications."""

FALLBACK = (
    "👋 Hi! I'm MediBuddy, your medication assistant.\n\n"
    "I can help you:\n"
    "• Add medications and set reminders\n"
    "• Track your remaining tablets\n"
    "• Alert you before you run out\n"
    "• Answer general medicine questions\n\n"
    "What would you like to do today?"
)

EDIT_INTERVAL = 0.8


async def respond(
    message: str,
    chat_id: str,
    history: list[dict] | None = None,
    placeholder_id: int | None = None,
) -> str:
    """Stream a conversational reply via message edits. Returns the full text."""
    try:
        message_id = placeholder_id or await send_message_get_id(
            chat_id, "💭 <i>Thinking...</i>"
        )

        full_text = ""
        last_edit = time.monotonic()

        async for chunk in llm.stream_reply(
            message=message,
            system_instruction=SYSTEM_INSTRUCTION,
            history=history,
        ):
            full_text += chunk
            now = time.monotonic()
            if message_id and now - last_edit >= EDIT_INTERVAL and full_text.strip():
                await edit_message(chat_id, message_id, full_text + " ✍️")
                last_edit = now

        final = full_text.strip() or FALLBACK
        if message_id:
            await edit_message(chat_id, message_id, final)
        return final

    except Exception:
        logger.exception("Conversational reply failed")
        return FALLBACK
