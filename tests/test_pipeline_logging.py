"""A successfully handled message must leave a trace in the logs."""
import logging

import pytest

from app.schemas import ListMedicationsIntent, TelegramMessage, TelegramUpdate, TelegramUser


def _update():
    return TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            message_id=1, chat={"id": 42}, text="show my medications",
            **{"from": TelegramUser(id=42)},
        ),
    )


async def test_handling_a_message_logs_what_happened(db, user, sent, monkeypatch, caplog):
    import app.routers.webhook as webhook_mod

    async def fake_parse(text):
        return [ListMedicationsIntent()]

    monkeypatch.setattr(webhook_mod, "parse_intent", fake_parse)

    with caplog.at_level(logging.INFO, logger="app.routers.webhook"):
        await webhook_mod._process_message_inner(_update())

    messages = " | ".join(r.getMessage() for r in caplog.records)
    assert "LIST_MEDICATIONS" in messages or "intent" in messages.lower(), messages
    assert "42" in messages, f"the user should be identifiable in the logs: {messages}"
