"""App construction, webhook authentication and rate-limit handling."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import WEBHOOK_SECRET

UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "from": {"id": 42},
        "chat": {"id": 42},
        "text": "hello",
    },
}
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture
def processed(monkeypatch):
    """Record dispatches instead of running the real pipeline."""
    seen = []
    import app.routers.webhook as webhook_mod

    async def fake_message(update):
        seen.append(("message", update))

    async def fake_callback(callback):
        seen.append(("callback", callback))

    monkeypatch.setattr(webhook_mod, "_process_message", fake_message)
    monkeypatch.setattr(webhook_mod, "_process_callback", fake_callback)
    return seen


@pytest.fixture
def client():
    from app.main import app
    from app.rate_limiter import limiter

    if hasattr(limiter._storage, "reset"):
        limiter._storage.reset()
    with TestClient(app) as c:
        yield c


def test_webhook_rejects_request_with_no_secret_header(client, processed):
    resp = client.post("/webhook", json=UPDATE)
    assert resp.status_code == 403
    assert processed == []


def test_webhook_rejects_request_with_wrong_secret(client, processed):
    resp = client.post("/webhook", json=UPDATE, headers={SECRET_HEADER: "not-the-secret"})
    assert resp.status_code == 403
    assert processed == []


def test_webhook_accepts_request_with_correct_secret(client, processed):
    resp = client.post("/webhook", json=UPDATE, headers={SECRET_HEADER: WEBHOOK_SECRET})
    assert resp.status_code == 200
    assert [kind for kind, _ in processed] == ["message"]


def test_app_exposes_limiter_state_for_the_rate_limit_handler():
    from app.main import app

    assert getattr(app.state, "limiter", None) is not None


def test_webhook_over_rate_limit_returns_200_so_telegram_does_not_retry(client, processed):
    headers = {SECRET_HEADER: WEBHOOK_SECRET}
    statuses = {client.post("/webhook", json=UPDATE, headers=headers).status_code for _ in range(35)}
    assert statuses == {200}, f"webhook must always answer 200, saw {statuses}"


async def test_rate_limit_handler_returns_429_for_non_webhook_paths():
    from slowapi.errors import RateLimitExceeded
    from starlette.datastructures import URL

    from app.main import rate_limit_handler

    class _Req:
        url = URL("http://test/dev/chat")
        client = None

    from types import SimpleNamespace

    limit = SimpleNamespace(error_message=None, limit="30 per 1 minute")
    resp = await rate_limit_handler(_Req(), RateLimitExceeded(limit))
    assert resp.status_code == 429


def test_dev_routes_are_not_mounted_when_debug_is_off():
    from app.main import app

    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/dev")]


def test_dev_routes_are_mounted_when_debug_is_on():
    from app.main import create_app

    app = create_app(debug=True)
    assert [r for r in app.routes if getattr(r, "path", "").startswith("/dev")]


def test_shutdown_closes_the_shared_http_client():
    from app.main import create_app
    from app.services import notification_service as notif

    notif._get_client()
    assert notif._client is not None

    with TestClient(create_app()) as c:
        c.get("/health")

    assert notif._client is None, "the pooled client must be closed on shutdown"


def test_request_body_logging_is_off_by_default(monkeypatch, tmp_path):
    """Request bodies contain user health data, so opting in must be explicit."""
    from app.config import Settings

    monkeypatch.delenv("LOG_REQUESTS", raising=False)
    # Ignore the developer's own .env, which may opt in.
    settings = Settings(telegram_bot_token="x", _env_file=str(tmp_path / "absent.env"))
    assert settings.log_requests is False
