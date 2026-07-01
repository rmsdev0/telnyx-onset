"""TTS client tests: text framing, MP3 decode to frames, and socket close."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    # This exercises the whole-buffer path specifically.
    client = TtsClient(_settings(tts_streaming_decode=False))
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


# ── Streaming decode path (settings.tts_streaming_decode=True) ────


def _fake_stream_mp3_frames(
    source: object, *, sample_rate: int, frame_bytes: int
) -> Iterator[bytes]:
    """Stand-in for the real miniaudio decode (which needs a real MP3).

    Drains the source so the queue + EOF bridge is exercised end to end, then
    yields one silence frame per 4 bytes of MP3 consumed.
    """
    consumed = bytearray()
    while True:
        chunk = source.read(4096)  # type: ignore[attr-defined]
        if not chunk:
            break
        consumed += chunk
    for _ in range(len(consumed) // 4):
        yield b"\x00" * frame_bytes


@pytest.mark.asyncio
async def test_streaming_synthesize_yields_frames(
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
    monkeypatch.setattr("onset.tts.stream_mp3_frames", _fake_stream_mp3_frames)

    client = TtsClient(_settings(tts_streaming_decode=True))
    frames = [f async for f in client.synthesize("hello world")]

    sent_texts = [json.loads(s)["text"] for s in conn.sent]
    assert sent_texts == [" ", "hello world", ""]
    assert conn.closed
    # 16 bytes of MP3 consumed -> 4 frames of frame_bytes (640).
    assert len(frames) == 4
    assert all(len(f) == 640 for f in frames)


@pytest.mark.asyncio
async def test_streaming_synthesize_closes_socket_on_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barge-in closes the generator mid-stream; the finally tears the socket down."""
    messages = [
        json.dumps({"audio": base64.b64encode(b"MP3PART1").decode(), "isFinal": True}),
    ]
    conn = _FakeTtsConn(messages)

    async def fake_connect(url: str, **kwargs: object) -> _FakeTtsConn:
        return conn

    monkeypatch.setattr("onset.tts.ws_connect", fake_connect)
    monkeypatch.setattr("onset.tts.stream_mp3_frames", _fake_stream_mp3_frames)

    client = TtsClient(_settings(tts_streaming_decode=True))
    agen = client.synthesize("hello")
    first = await agen.__anext__()
    assert len(first) == 640
    await agen.aclose()
    assert conn.closed


@pytest.mark.asyncio
async def test_streaming_decode_failure_before_any_frame_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decode error with no audio emitted surfaces, so _speak can fall back."""
    messages = [
        json.dumps({"audio": base64.b64encode(b"BADMP3").decode(), "isFinal": True}),
    ]
    conn = _FakeTtsConn(messages)

    async def fake_connect(url: str, **kwargs: object) -> _FakeTtsConn:
        return conn

    def boom(source: object, *, sample_rate: int, frame_bytes: int) -> Iterator[bytes]:
        while source.read(4096):  # type: ignore[attr-defined]
            pass
        raise RuntimeError("bad mp3")
        yield b""  # pragma: no cover - unreachable, marks this a generator

    monkeypatch.setattr("onset.tts.ws_connect", fake_connect)
    monkeypatch.setattr("onset.tts.stream_mp3_frames", boom)

    client = TtsClient(_settings(tts_streaming_decode=True))
    with pytest.raises(RuntimeError, match="bad mp3"):
        _ = [f async for f in client.synthesize("hello")]
    assert conn.closed
