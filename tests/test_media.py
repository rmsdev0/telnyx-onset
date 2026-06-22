"""Media transport tests: inbound decode and the outbound paced injector."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from onset.media import (
    Connected,
    Dtmf,
    Ignore,
    Mark,
    Media,
    MediaStream,
    Start,
    Stop,
    decode,
)


class FakeWS:
    """Records the JSON messages MediaStream sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def kinds(self, event: str) -> list[dict[str, object]]:
        return [m for m in self.sent if m.get("event") == event]


def _media_msg(pcm: bytes) -> str:
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(pcm).decode()}}
    )


def test_decode_inbound_shapes() -> None:
    assert isinstance(
        decode(json.dumps({"event": "connected", "version": "1.0.0"})), Connected
    )
    start = decode(
        json.dumps(
            {
                "event": "start",
                "start": {"call_control_id": "cc", "from": "+1"},
                "stream_id": "s1",
            }
        )
    )
    assert isinstance(start, Start)
    assert start.call_control_id == "cc"
    assert start.from_number == "+1"
    assert start.stream_id == "s1"

    m = decode(_media_msg(b"\x01\x02\x03\x04"))
    assert isinstance(m, Media)
    assert m.pcm16 == b"\x01\x02\x03\x04"

    assert isinstance(
        decode(json.dumps({"event": "mark", "mark": {"name": "speak:3"}})), Mark
    )
    assert isinstance(decode(json.dumps({"event": "stop"})), Stop)
    assert isinstance(
        decode(json.dumps({"event": "dtmf", "dtmf": {"digit": "5"}})), Dtmf
    )
    assert isinstance(
        decode(json.dumps({"event": "error", "payload": {"code": 1}})), Ignore
    )
    assert isinstance(decode("not json at all"), Ignore)
    assert isinstance(decode(json.dumps({"event": "weird"})), Ignore)


@pytest.mark.asyncio
async def test_pacer_injects_frames_and_mark() -> None:
    ws = FakeWS()
    media = MediaStream(ws, frame_ms=1, lead_frames=10)
    media.start()
    epoch = media.begin_utterance()
    for _ in range(3):
        await media.send_audio_frame(epoch, b"\xaa\xbb")
    await media.send_mark(epoch, "speak:1")
    await asyncio.sleep(0.08)

    media_msgs = ws.kinds("media")
    mark_msgs = ws.kinds("mark")
    assert len(media_msgs) == 3
    payload = media_msgs[0]["media"]["payload"]  # type: ignore[index]
    assert payload == base64.b64encode(b"\xaa\xbb").decode()
    assert mark_msgs
    assert mark_msgs[0]["mark"]["name"] == "speak:1"  # type: ignore[index]
    await media.aclose()


@pytest.mark.asyncio
async def test_flush_clears_and_drops_inflight() -> None:
    ws = FakeWS()
    # Slow pacer so frames sit in the queue when flush arrives.
    media = MediaStream(ws, frame_ms=50, lead_frames=100)
    media.start()
    epoch = media.begin_utterance()
    for _ in range(20):
        await media.send_audio_frame(epoch, b"\x00\x00")

    await media.flush()
    assert ws.kinds("clear")
    sent_before = len(ws.kinds("media"))

    # A frame tagged with the now-stale epoch is dropped by the producer guard.
    await media.send_audio_frame(epoch, b"\x11\x11")
    await asyncio.sleep(0.12)
    assert len(ws.kinds("media")) == sent_before
    await media.aclose()


@pytest.mark.asyncio
async def test_aclose_stops_pacer_and_is_idempotent() -> None:
    ws = FakeWS()
    media = MediaStream(ws, frame_ms=1, lead_frames=10)
    media.start()
    await media.aclose()
    assert media._pacer_task is not None
    assert media._pacer_task.done()
    await media.aclose()  # idempotent


class _FailingWS:
    async def send_text(self, data: str) -> None:
        raise ConnectionError("send failed")


@pytest.mark.asyncio
async def test_pacer_failure_fires_on_error() -> None:
    # A send failure in the pacer must wind the call down via on_error, not die
    # silently and wedge the producer for the rest of the call.
    errors: list[int] = []
    media = MediaStream(_FailingWS(), frame_ms=1, lead_frames=10)
    media.on_error = lambda: errors.append(1)
    media.start()
    epoch = media.begin_utterance()
    await media.send_audio_frame(epoch, b"\x00\x00")
    for _ in range(200):
        if errors:
            break
        await asyncio.sleep(0.005)
    assert errors == [1]
    assert media._closed
    await media.aclose()
