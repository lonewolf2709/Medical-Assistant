"""Telegram delivery: retry policy and failure reporting."""
import httpx
import pytest
from tenacity import wait_none

from app.services import notification_service as notif


def _client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def no_wait():
    """Strip the backoff so retry behaviour can be tested without sleeping."""
    return lambda fn: fn.retry_with(wait=wait_none())


async def test_send_message_retries_a_server_error_then_succeeds(monkeypatch, no_wait):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    monkeypatch.setattr(notif, "_get_client", lambda: _client_returning(handler))

    assert await no_wait(notif.send_message)("42", "hi") is True
    assert len(attempts) == 3


async def test_send_message_reports_failure_instead_of_raising(monkeypatch, no_wait):
    def handler(request):
        return httpx.Response(500, json={"ok": False})

    monkeypatch.setattr(notif, "_get_client", lambda: _client_returning(handler))

    result = await no_wait(notif.send_message)("42", "hi")
    assert not result, "exhausted retries must report failure, not raise"


async def test_send_message_does_not_retry_a_bad_request(monkeypatch, no_wait):
    """A 400 (e.g. malformed HTML) will never succeed — retrying it wastes time."""
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})

    monkeypatch.setattr(notif, "_get_client", lambda: _client_returning(handler))

    result = await no_wait(notif.send_message)("42", "<b>bad")
    assert not result
    assert len(attempts) == 1, f"a 400 was retried {len(attempts)} times"


async def test_connection_errors_are_retried(monkeypatch, no_wait):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 2:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(notif, "_get_client", lambda: _client_returning(handler))

    assert await no_wait(notif.send_message)("42", "hi") is True
    assert len(attempts) == 2


async def test_the_http_client_is_reused_across_calls(monkeypatch):
    """A fresh AsyncClient per call throws away the connection pool."""
    notif._client = None
    first = notif._get_client()
    second = notif._get_client()
    assert first is second


def test_the_connect_timeout_is_generous_and_configurable():
    """A tight connect budget turns an event-loop stall into a spurious
    ConnectTimeout even when the network is fine."""
    from app.config import Settings
    from app.services import notification_service as notif

    defaults = Settings(telegram_bot_token="x")
    assert defaults.telegram_connect_timeout_seconds >= 10
    assert notif.TIMEOUT.connect == defaults.telegram_connect_timeout_seconds
