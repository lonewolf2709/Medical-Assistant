import functools
import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
TIMEOUT = httpx.Timeout(
    settings.telegram_timeout_seconds,
    connect=settings.telegram_connect_timeout_seconds,
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """One shared client, so connections are pooled across the many calls per message."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport failures and Telegram's 5xx/429 — never a 4xx.

    A 400 ("can't parse entities", chat not found, bot blocked) will fail
    identically every time, so retrying only delays the failure.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def _log_failure(retry_state) -> None:
    """Called once retries are exhausted — report failure rather than raising.

    Callers treat a falsy result as "not delivered": the scheduler leaves the
    reminder pending so the next poll tries again.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.error(
        "Telegram call %s failed after %d attempts: %s",
        retry_state.fn.__name__ if retry_state.fn else "?",
        retry_state.attempt_number,
        exc,
    )
    return None


_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    retry_error_callback=_log_failure,
)


def _telegram_call(fn):
    """Retry transient failures; report permanent ones as a falsy result.

    A rejected call (bad HTML, chat not found, bot blocked) must not propagate:
    callers are background tasks whose only sensible response is to log it.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPStatusError as exc:
            if _is_retryable(exc):
                raise
            logger.error(
                "Telegram rejected %s: %s %s",
                fn.__name__,
                exc.response.status_code,
                exc.response.text[:300],
            )
            return None

    return _retry(wrapper)


async def _post(method: str, payload: dict) -> httpx.Response:
    resp = await _get_client().post(f"{TELEGRAM_API}/{method}", json=payload, timeout=TIMEOUT)
    # Raise so the retry policy can decide — non-2xx was previously ignored, which
    # meant Telegram's 5xx responses were never retried.
    resp.raise_for_status()
    return resp


@_telegram_call
async def send_message(chat_id: str, text: str) -> bool:
    await _post("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    return True


@_telegram_call
async def send_message_get_id(chat_id: str, text: str) -> int | None:
    """Send a message and return the message_id for later editing."""
    resp = await _post("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    return resp.json().get("result", {}).get("message_id")


@_telegram_call
async def edit_message(chat_id: str, message_id: int, text: str) -> bool:
    """Edit an existing message — used for the streaming typewriter effect."""
    await _post(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
    )
    return True


@_telegram_call
async def send_message_with_buttons(chat_id: str, text: str, buttons: list[list[dict]]) -> int | None:
    """Send a message with an inline keyboard. Returns the message_id."""
    resp = await _post(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        },
    )
    return resp.json().get("result", {}).get("message_id")


@_telegram_call
async def delete_message(chat_id: str, message_id: int) -> bool:
    await _post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    return True


async def send_typing(chat_id: str) -> None:
    """Show the 'typing...' indicator — best effort, no retry."""
    try:
        await _post("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        logger.debug("sendChatAction failed", exc_info=True)


async def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback query — best effort, no retry."""
    try:
        await _post(
            "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}
        )
    except Exception:
        logger.debug("answerCallbackQuery failed", exc_info=True)
