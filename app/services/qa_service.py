"""Q&A service: streams Gemini output as a typewriter effect via message edits."""
import logging
import time

from app.services import llm
from app.services.notification_service import edit_message, send_message_get_id

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTION = """You are MediBuddy, a knowledgeable and caring medication assistant.

You can answer:
- General medication questions (uses, side effects, interactions, dosage info)
- Symptom-based queries — suggest common OTC medications people can buy without a prescription
  e.g. "I have a fever" → suggest Paracetamol (500mg), Ibuprofen
  e.g. "I have a cold" → suggest Cetirizine, Loratadine, saline nasal spray
  e.g. "I have acidity" → suggest Pantoprazole, Omeprazole, antacids like Gelusil
  Always mention the common brand names where relevant (e.g. Crocin, Dolo, Allegra)
  Always include typical adult dosage if well-known

You MUST:
- Always end symptom responses with: "⚠️ These are common OTC suggestions. Please consult a doctor if symptoms persist or worsen."
- Decline questions asking for clinical diagnosis, prescription-only drug recommendations, or personalized treatment plans
- If declining, respond with exactly: OUT_OF_SCOPE

Keep responses concise and practical. Use bullet points for medication lists."""

DISCLAIMER = "\n\n⚠️ This is general information only. Consult a healthcare professional for medical advice."
EDIT_INTERVAL = 0.8  # seconds between edits to avoid Telegram rate limits


async def answer_question(
    question: str,
    chat_id: str,
    history: list[dict] | None = None,
    placeholder_id: int | None = None,
) -> str:
    """Stream the answer to Telegram via message edits. Returns the full text."""
    try:
        # Reuse the caller's placeholder if it sent one.
        message_id = placeholder_id or await send_message_get_id(
            chat_id, "💭 <i>Thinking...</i>"
        )

        full_text = ""
        last_edit = time.monotonic()

        async for chunk in llm.stream_reply(
            message=question,
            system_instruction=SYSTEM_INSTRUCTION,
            history=history,
        ):
            full_text += chunk
            now = time.monotonic()
            # Edit at most every EDIT_INTERVAL to stay under Telegram's limits.
            if message_id and now - last_edit >= EDIT_INTERVAL and full_text.strip():
                await edit_message(chat_id, message_id, full_text + " ✍️")
                last_edit = now

        if full_text.strip() == "OUT_OF_SCOPE":
            final = (
                "I can't provide medical advice for that. "
                "Please consult a healthcare professional."
            )
        else:
            final = full_text.strip() + DISCLAIMER

        if message_id:
            await edit_message(chat_id, message_id, final)
        return final

    except Exception:
        logger.exception("Q&A generation failed")
        return "Sorry, I couldn't process your question. Please try again."
