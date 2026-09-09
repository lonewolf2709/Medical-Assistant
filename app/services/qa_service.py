"""Q&A service using Gemini streaming with typewriter effect via Telegram message edits."""
import asyncio
import time

import google.generativeai as genai

from app.config import settings
from app.services.notification_service import edit_message, send_message_get_id

genai.configure(api_key=settings.gemini_api_key)

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


async def answer_question(question: str, chat_id: str, history: list[dict] | None = None, placeholder_id: int | None = None) -> str:
    """Stream Gemini response to Telegram via message edits. Returns full text."""
    model = genai.GenerativeModel(settings.gemini_model, system_instruction=SYSTEM_INSTRUCTION)

    try:
        chat = model.start_chat(history=history or [])
        # Reuse existing placeholder or send a new one
        if placeholder_id:
            message_id = placeholder_id
        else:
            message_id = await send_message_get_id(chat_id, "💭 <i>Thinking...</i>")
        if not message_id:
            # Fallback to non-streaming
            response = await chat.send_message_async(question)
            answer = (response.text or "").strip()
            if answer == "OUT_OF_SCOPE":
                return "I can't provide medical advice for that. Please consult a healthcare professional."
            return answer + DISCLAIMER

        # Stream from Gemini
        full_text = ""
        last_edit = time.monotonic()

        async for chunk in await chat.send_message_async(question, stream=True):
            if chunk.text:
                full_text += chunk.text
                now = time.monotonic()
                # Edit every EDIT_INTERVAL seconds to avoid rate limits
                if now - last_edit >= EDIT_INTERVAL and full_text.strip():
                    await edit_message(chat_id, message_id, full_text + " ✍️")
                    last_edit = now

        # Final edit with complete response
        if full_text.strip() == "OUT_OF_SCOPE":
            final = "I can't provide medical advice for that. Please consult a healthcare professional."
        else:
            final = full_text.strip() + DISCLAIMER

        await edit_message(chat_id, message_id, final)
        return final

    except Exception:
        fallback = "Sorry, I couldn't process your question. Please try again."
        return fallback
