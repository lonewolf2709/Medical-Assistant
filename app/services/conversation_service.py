"""Conversational responses with streaming typewriter effect."""
import time

import google.generativeai as genai

from app.config import settings
from app.services.notification_service import edit_message, send_message_get_id

genai.configure(api_key=settings.gemini_api_key)

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


async def respond(message: str, chat_id: str, history: list[dict] | None = None, placeholder_id: int | None = None) -> str:
    """Stream conversational response to Telegram via message edits. Returns full text."""
    model = genai.GenerativeModel(settings.gemini_model, system_instruction=SYSTEM_INSTRUCTION)
    try:
        chat = model.start_chat(history=history or [])
        if placeholder_id:
            message_id = placeholder_id
        else:
            message_id = await send_message_get_id(chat_id, "💭 <i>Thinking...</i>")
        if not message_id:
            response = await chat.send_message_async(message)
            return (response.text or "").strip() or FALLBACK

        full_text = ""
        last_edit = time.monotonic()

        async for chunk in await chat.send_message_async(message, stream=True):
            if chunk.text:
                full_text += chunk.text
                now = time.monotonic()
                if now - last_edit >= EDIT_INTERVAL and full_text.strip():
                    await edit_message(chat_id, message_id, full_text + " ✍️")
                    last_edit = now

        final = full_text.strip() or FALLBACK
        await edit_message(chat_id, message_id, final)
        return final

    except Exception:
        return FALLBACK
