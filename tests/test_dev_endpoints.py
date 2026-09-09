"""The debug-only endpoints must actually work when they are mounted."""
import pytest
from fastapi.testclient import TestClient

from app.schemas import ListMedicationsIntent


@pytest.fixture
def dev_client(monkeypatch):
    async def fake_parse(text):
        return [ListMedicationsIntent()]

    import app.routers.dev as dev_mod
    import app.services.parser as parser_mod

    monkeypatch.setattr(parser_mod, "parse_intent", fake_parse)
    monkeypatch.setattr(dev_mod, "parse_intent", fake_parse)

    # The autouse schema fixture pooled connections on another event loop.
    import asyncio

    from app.database import engine

    asyncio.run(engine.dispose())

    from app.main import create_app

    with TestClient(create_app(debug=True)) as c:
        yield c


def test_dev_parse_returns_all_intents(dev_client):
    resp = dev_client.post("/dev/parse", json={"text": "show my meds"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["intents"] == [{"intent": "LIST_MEDICATIONS"}]


def test_dev_chat_returns_a_reply(dev_client):
    resp = dev_client.post("/dev/chat", json={"telegram_id": "42", "text": "show my meds"})
    assert resp.status_code == 200, resp.text
    assert "reply" in resp.json()
