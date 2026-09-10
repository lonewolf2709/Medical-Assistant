"""The single seam onto the Gemini SDK."""
import pathlib
from types import SimpleNamespace

import pytest


class FakeModels:
    def __init__(self, text="hello"):
        self.calls = []
        self._text = text

    async def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=self._text)


class FakeChat:
    def __init__(self, chunks):
        self._chunks = chunks
        self.messages = []

    async def send_message_stream(self, message):
        self.messages.append(message)

        async def gen():
            for c in self._chunks:
                yield SimpleNamespace(text=c)

        return gen()

    async def send_message(self, message):
        self.messages.append(message)
        return SimpleNamespace(text="".join(self._chunks))


class FakeChats:
    def __init__(self, chunks):
        self.created = []
        self.chat = FakeChat(chunks)

    def create(self, *, model, config=None, history=None):
        self.created.append({"model": model, "config": config, "history": history})
        return self.chat


def fake_client(text="hello", chunks=("a", "b")):
    models, chats = FakeModels(text), FakeChats(list(chunks))
    return SimpleNamespace(aio=SimpleNamespace(models=models, chats=chats)), models, chats


async def test_generate_text_uses_the_configured_model_and_returns_the_text(monkeypatch):
    from app.config import settings
    from app.services import llm

    client, models, _ = fake_client(text="  spaced  ")
    monkeypatch.setattr(llm, "get_client", lambda: client)

    assert await llm.generate_text("hi") == "spaced"
    assert models.calls[0]["model"] == settings.gemini_model
    assert models.calls[0]["contents"] == "hi"


async def test_media_is_sent_as_raw_bytes_not_base64(monkeypatch):
    """The retired SDK took base64 in a dict; this one takes raw bytes. Sending
    base64 here would ship the encoded string as if it were audio."""
    from app.services import llm

    client, models, _ = fake_client(text="transcript")
    monkeypatch.setattr(llm, "get_client", lambda: client)

    out = await llm.generate_from_media(data=b"\x00\x01raw", mime_type="audio/ogg", prompt="go")

    assert out == "transcript"
    part = models.calls[0]["contents"][0]
    assert part.inline_data.data == b"\x00\x01raw", "raw bytes must be passed through"
    assert part.inline_data.mime_type == "audio/ogg"
    assert models.calls[0]["contents"][1] == "go"


def test_history_with_plain_string_parts_is_normalised():
    """message_log_service yields {"parts": ["text"]}, which the new SDK rejects."""
    from app.services import llm

    out = llm.normalise_history([
        {"role": "user", "parts": ["hello"]},
        {"role": "model", "parts": [{"text": "hi there"}]},
    ])

    assert out == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hi there"}]},
    ]


def test_normalise_history_tolerates_nothing():
    from app.services import llm

    assert llm.normalise_history(None) == []


async def test_stream_reply_yields_each_chunk_of_text(monkeypatch):
    from app.services import llm

    client, _, chats = fake_client(chunks=("Take ", "one ", "tablet"))
    monkeypatch.setattr(llm, "get_client", lambda: client)

    chunks = [c async for c in llm.stream_reply(message="q", system_instruction="be brief")]

    assert chunks == ["Take ", "one ", "tablet"]
    assert chats.created[0]["config"].system_instruction == "be brief"
    assert chats.chat.messages == ["q"]


async def test_stream_reply_passes_normalised_history(monkeypatch):
    from app.services import llm

    client, _, chats = fake_client()
    monkeypatch.setattr(llm, "get_client", lambda: client)

    [c async for c in llm.stream_reply(
        message="q", history=[{"role": "user", "parts": ["earlier"]}]
    )]

    assert chats.created[0]["history"] == [{"role": "user", "parts": [{"text": "earlier"}]}]


def test_no_module_still_imports_the_retired_sdk():
    import re

    pattern = re.compile(r"^\s*(?:import|from)\s+google\.generativeai", re.MULTILINE)
    offenders = [
        str(p) for p in pathlib.Path("app").rglob("*.py")
        if pattern.search(p.read_text())
    ]
    assert offenders == [], f"still importing the retired SDK: {offenders}"
