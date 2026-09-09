"""Voice message handling: download from Telegram + transcribe via Gemini."""
import base64
import logging

import google.generativeai as genai
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)
_model = genai.GenerativeModel(settings.gemini_model)

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def get_voice_bytes(file_id: str) -> bytes | None:
    """Call Telegram getFile to get file_path, then download the audio bytes."""
    async with httpx.AsyncClient() as client:
        # Step 1: get file_path from file_id
        resp = await client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10)
        if resp.status_code != 200:
            logger.error("getFile failed: %s", resp.text)
            return None
        file_path = resp.json().get("result", {}).get("file_path")
        if not file_path:
            return None

        # Step 2: download the audio
        download_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
        audio_resp = await client.get(download_url, timeout=30)
        if audio_resp.status_code != 200:
            logger.error("Audio download failed: %s", audio_resp.status_code)
            return None
        return audio_resp.content


async def transcribe(audio_bytes: bytes) -> str | None:
    """Send OGG audio bytes to Gemini and return transcribed text."""
    try:
        audio_b64 = base64.b64encode(audio_bytes).decode()
        response = await _model.generate_content_async([
            {"mime_type": "audio/ogg", "data": audio_b64},
            "Transcribe this voice message exactly as spoken. Return only the transcribed text, nothing else."
        ])
        text = (response.text or "").strip()
        return text if text else None
    except Exception:
        logger.exception("Gemini transcription failed")
        return None
