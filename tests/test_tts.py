"""TTS client tests: text framing, MP3 decode to frames, and socket close."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from onset.settings import Settings
from onset.tts import TtsClient


def _settings(**kw: object) -> Settings:
    return Settings(telnyx_api_key="k", telnyx_public_key="p", **kw)  # type: ignore[arg-type]


class _FakeTtsConn:
    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.closed = False
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._messages:
            await asyncio.sleep(0)
            return self._messages.pop(0)
        await asyncio.sleep(5)  # not reached: isFinal ends the loop
        return ""

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_synthesize_sends_text_and_yields_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [
        json.dumps({"audio": base64.b64encode(b"MP3PART1").decode(), "isFinal": False}),
        json.dumps({"audio": base64.b64encode(b"MP3PART2").decode(), "isFinal": True}),
    ]
    conn = _FakeTtsConn(messages)

    async def fake_connect(url: str, **kwargs: object) -> _FakeTtsConn:
        return conn

    monkeypatch.setattr("onset.tts.ws_connect", fake_connect)
    # Avoid a real MP3 decode: return three frames' worth of PCM (frame_bytes=640).
    fake_pcm = b"\x00" * (640 * 3)
    monkeypatch.setattr(
        "onset.tts.decode_mp3_to_pcm16", lambda mp3, rate: fake_pcm
    )

    client = TtsClient(_settings())
    frames = [f async for f in client.synthesize("hello world")]

    sent_texts = [json.loads(s)["text"] for s in conn.sent]
    assert sent_texts == [" ", "hello world", ""]
    assert conn.closed
    assert len(frames) == 3
    assert all(len(f) == 640 for f in frames)


@pytest.mark.asyncio
async def test_synthesize_empty_text_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_connect(url: str, **kwargs: object) -> object:
        raise AssertionError("should not connect for empty text")

    monkeypatch.setattr("onset.tts.ws_connect", fake_connect)
    client = TtsClient(_settings())
    frames = [f async for f in client.synthesize("   ")]
    assert frames == []
