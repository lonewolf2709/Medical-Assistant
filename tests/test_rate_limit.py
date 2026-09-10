"""Rate limiting must be per Telegram user, not per source IP.

Every update arrives from Telegram's own servers, so an IP-keyed limit puts all
users in one bucket and lets a single chatty user throttle everybody.
"""
import pytest


def test_a_user_over_their_limit_is_rejected():
    from app.rate_limiter import UserRateLimiter

    limiter = UserRateLimiter(max_events=3, window_seconds=60)

    assert [limiter.allow("user-a", now=0) for _ in range(3)] == [True, True, True]
    assert limiter.allow("user-a", now=0) is False


def test_one_users_flood_does_not_affect_another_user():
    from app.rate_limiter import UserRateLimiter

    limiter = UserRateLimiter(max_events=3, window_seconds=60)
    for _ in range(10):
        limiter.allow("noisy", now=0)

    assert limiter.allow("quiet", now=0) is True, "users must not share a bucket"


def test_the_window_slides_so_the_limit_recovers():
    from app.rate_limiter import UserRateLimiter

    limiter = UserRateLimiter(max_events=2, window_seconds=60)
    limiter.allow("u", now=0)
    limiter.allow("u", now=0)
    assert limiter.allow("u", now=30) is False
    assert limiter.allow("u", now=61) is True


def test_webhook_drops_a_flooding_user_with_200_so_telegram_does_not_retry(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.rate_limiter import limiter, user_limiter
    from tests.conftest import WEBHOOK_SECRET

    if hasattr(limiter._storage, "reset"):
        limiter._storage.reset()
    user_limiter.reset()
    monkeypatch.setattr(user_limiter, "max_events", 2)

    seen = []
    import app.routers.webhook as webhook_mod

    async def fake(update):
        seen.append(update)

    monkeypatch.setattr(webhook_mod, "_process_message", fake)

    update = {"update_id": 1, "message": {"message_id": 1, "from": {"id": 42},
                                          "chat": {"id": 42}, "text": "hi"}}
    with TestClient(app) as client:
        codes = [
            client.post("/webhook", json=update,
                        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}).status_code
            for _ in range(5)
        ]

    assert set(codes) == {200}, codes
    assert len(seen) == 2, f"only 2 should have been processed, got {len(seen)}"
