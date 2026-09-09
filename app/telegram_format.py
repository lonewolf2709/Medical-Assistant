"""Helpers for building Telegram messages sent with parse_mode=HTML."""
import html


def esc(value: object) -> str:
    """Escape text for Telegram's HTML parse mode.

    Medication names and price strings come from users and from the LLM. An
    unescaped "<" or "&" makes Telegram reject the whole message with a 400,
    so the user silently receives nothing.
    """
    return html.escape(str(value), quote=False)
