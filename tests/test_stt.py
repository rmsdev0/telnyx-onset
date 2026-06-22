"""STT client tests: transcript parsing, bounded feed, and socket lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest

from onset.settings import Settings
from onset.stt import SttClient


def _settings(**kw: object) -> Settings:
    return Settings(telnyx_api_key="k", telnyx_public_key="p", **kw)  # type: ignore[arg-type]


def test_handle_message_maps_transcripts() -> None:
    seen: list[tuple[str, bool, bool]] = []
    client = SttClient(
        _settings(), on_transcript=lambda t, f, s: seen.append((t, f, s))
    )
    client._handle_message(
        json.dumps({"transcript": "hi", "is_final": False, "speech_final": False})
    )
    client._handle_message(
        json.dumps(
            {"transcript": "hello there", "is_final": True, "speech_final": False}
        )
    )
    client._handle_message(
        json.dumps({"transcript": "done", "is_final": True, "speech_final": True})
    )
    # The empty end-of-utterance frame still drives the turn end; without it,
    # speech_final never produces an UTTERANCE_END and turns never complete.
    client._handle_message(
        json.dumps({"transcript": "", "is_final": True, "speech_final": True})
    )
    # A truly empty (non-endpoint) frame and error frames are ignored.
    client._handle_message(
        json.dumps({"transcript": "", "is_final": False, "speech_final": False})
    )
    client._handle_message(json.dumps({"errors": [{"code": "x"}]}))

    assert seen == [
        ("hi", False, False),
        ("hello there", True, False),
        ("done", True, True),
        ("", True, True),
    ]


def test_feed_drops_oldest_when_full() -> None:
    client = SttClient(_settings(stt_feed_max_frames=3), on_transcript=lambda *a: None)
    for i in range(6):
        client.feed(bytes([i]))
    assert client._feed.qsize() == 3
    kept = [client._feed.get_nowait() for _ in range(3)]
    assert kept == [bytes([3]), bytes([4]), bytes([5])]


class _FakeConn:
    """A minimal websockets connection: async-iterable, with send and close."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.closed = False
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> _FakeConn:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            await asyncio.sleep(0)
            return self._messages.pop(0)
        while not self.closed:
            await asyncio.sleep(0.01)
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_connect_streams_then_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, bool, bool]] = []
    conn = _FakeConn(
        [json.dumps({"transcript": "open up", "is_final": True, "speech_final": True})]
    )

    async def fake_connect(url: str, **kwargs: object) -> _FakeConn:
        return conn

    monkeypatch.setattr("onset.stt.ws_connect", fake_connect)

    client = SttClient(
        _settings(), on_transcript=lambda t, f, s: seen.append((t, f, s))
    )
    await client.connect()
    client.feed(b"\x00\x00")
    await _until(lambda: bool(seen) and bool(conn.sent))
    await client.aclose()

    assert conn.closed
    assert seen[0] == ("open up", True, True)


class _DroppingConn:
    """A connection that drops immediately on iteration (socket failure)."""

    def __init__(self) -> None:
        self.closed = False

    async def send(self, data: bytes) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> _DroppingConn:
        return self

    async def __anext__(self) -> str:
        raise ConnectionError("dropped")


@pytest.mark.asyncio
async def test_reconnect_exhaustion_fires_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Initial connect succeeds but the socket drops, and every reopen fails. After
    # the reconnect budget is spent, on_error must fire so the agent winds down,
    # rather than the loop dying after one attempt and the call going deaf.
    errors: list[int] = []
    calls = {"n": 0}

    async def fake_connect(url: str, **kwargs: object) -> _DroppingConn:
        calls["n"] += 1
        if calls["n"] == 1:
            return _DroppingConn()
        raise ConnectionError("reopen failed")

    monkeypatch.setattr("onset.stt.ws_connect", fake_connect)

    client = SttClient(
        _settings(stt_max_reconnects=2, stt_reconnect_backoff_s=0.001),
        on_transcript=lambda *a: None,
        on_error=lambda: errors.append(1),
    )
    await client.connect()
    await _until(lambda: bool(errors))
    await client.aclose()

    assert errors == [1]
    assert calls["n"] >= 3  # initial connect + 2 reopen attempts


async def _until(pred: object, timeout: float = 2.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():  # type: ignore[operator]
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timeout")
