"""The single seam onto the Gemini SDK.

Every model call goes through here, so the SDK is named in exactly one module.
The previous `google.generativeai` package reached end of life and its async
methods wrapped blocking calls, which starved the event loop and surfaced as
spurious `ConnectTimeout`s on healthy networks.
"""
import logging
from collections.abc import AsyncIterator, Iterable

from google import genai
from google.genai import types

from app.config import settings

logger = logging.getLogger(__name__)

_client: genai.Client | None = None

# We never pass tools, so automatic function calling has nothing to do. Disabling
# it silences the SDK's "direct use of AFC is not recommended" notice.
_NO_TOOLS = types.AutomaticFunctionCallingConfig(disable=True)


def get_client() -> genai.Client:
    """One lazily-built client, reused across calls."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def normalise_history(history: Iterable[dict] | None) -> list[dict]:
    """Coerce stored chat history into the shape the SDK accepts.

    `message_log_service` stores parts as bare strings (`{"parts": ["hi"]}`),
    which this SDK rejects — each part must be an object (`{"text": "hi"}`).
    """
    if not history:
        return []

    normalised: list[dict] = []
    for turn in history:
        parts = [
            part if isinstance(part, dict) else {"text": str(part)}
            for part in turn.get("parts", [])
        ]
        normalised.append({"role": turn.get("role", "user"), "parts": parts})
    return normalised


async def generate_text(prompt: str, *, model: str | None = None) -> str:
    """One-shot text generation."""
    response = await get_client().aio.models.generate_content(
        model=model or settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(automatic_function_calling=_NO_TOOLS),
    )
    return (response.text or "").strip()


async def generate_from_media(
    *, data: bytes, mime_type: str, prompt: str, model: str | None = None
) -> str:
    """Generate from a media payload plus a prompt.

    `data` is raw bytes. The retired SDK expected base64 inside a dict; passing
    base64 here would send the encoded text as though it were the media.
    """
    response = await get_client().aio.models.generate_content(
        model=model or settings.gemini_model,
        contents=[types.Part.from_bytes(data=data, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(automatic_function_calling=_NO_TOOLS),
    )
    return (response.text or "").strip()


async def stream_reply(
    *,
    message: str,
    system_instruction: str | None = None,
    history: Iterable[dict] | None = None,
    model: str | None = None,
) -> AsyncIterator[str]:
    """Stream a chat reply, yielding text as it arrives."""
    config = (
        types.GenerateContentConfig(system_instruction=system_instruction)
        if system_instruction
        else None
    )
    chat = get_client().aio.chats.create(
        model=model or settings.gemini_model,
        config=config,
        history=normalise_history(history),
    )
    # send_message_stream is a coroutine that resolves to the async iterator.
    stream = await chat.send_message_stream(message)
    async for chunk in stream:
        if chunk.text:
            yield chunk.text
