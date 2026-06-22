"""The call media WebSocket: inbound decode, paced outbound injection, flush.

Telnyx dials this server's media route and runs one bidirectional RTP stream per
call. Inbound caller audio arrives as base64 L16 in media events; outbound audio
is injected as media events on the same socket; a clear event flushes whatever
Telnyx has queued, which is the frame-level barge-in primitive.

decode() normalizes the inbound JSON for the server's read loop. MediaStream is
the outbound side (the agent's TTS injection): audio is paced at one frame per
frame_ms through a bounded queue so Telnyx never buffers more than a shallow
lead, and an epoch guards the queue so flush() drops every frame of the
interrupted utterance, including any the producer enqueues during the flush.
Outbound media and clear carry no stream_id (one stream per socket).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from onset.audio import decode_l16_payload, encode_l16_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from onset.transport import WebSocketLike

log = structlog.get_logger()


# ── Inbound events (decoded from Telnyx) ─────────────────────────


@dataclass(frozen=True, slots=True)
class Connected:
    version: str


@dataclass(frozen=True, slots=True)
class Start:
    call_control_id: str
    from_number: str
    stream_id: str


@dataclass(frozen=True, slots=True)
class Media:
    pcm16: bytes


@dataclass(frozen=True, slots=True)
class Mark:
    name: str


@dataclass(frozen=True, slots=True)
class Stop:
    pass


@dataclass(frozen=True, slots=True)
class Dtmf:
    digit: str


@dataclass(frozen=True, slots=True)
class Ignore:
    reason: str


InboundEvent = Connected | Start | Media | Mark | Stop | Dtmf | Ignore


def decode(raw: str) -> InboundEvent:
    """Normalize a raw Telnyx media-socket JSON message into an event."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return Ignore(reason="unparseable")
    if not isinstance(data, dict):
        return Ignore(reason="not_an_object")
    event = data.get("event")
    if event == "connected":
        return Connected(version=str(data.get("version", "")))
    if event == "start":
        start = data.get("start") or {}
        return Start(
            call_control_id=str(start.get("call_control_id", "")),
            from_number=str(start.get("from", "")),
            stream_id=str(data.get("stream_id", "")),
        )
    if event == "media":
        payload = (data.get("media") or {}).get("payload", "")
        try:
            pcm = decode_l16_payload(payload)
        except (ValueError, TypeError):
            return Ignore(reason="bad_media_payload")
        return Media(pcm16=pcm)
    if event == "mark":
        return Mark(name=str((data.get("mark") or {}).get("name", "")))
    if event == "stop":
        return Stop()
    if event == "dtmf":
        return Dtmf(digit=str((data.get("dtmf") or {}).get("digit", "")))
    if event == "error":
        return Ignore(reason=f"error:{data.get('payload', {})}")
    return Ignore(reason=f"unknown:{event}")


# Outbound queue item kinds.
_AUDIO = "audio"
_MARK = "mark"

_QueueItem = tuple[int, str, bytes]


class MediaStream:
    """Outbound injector for one call's media socket: paced audio, clear, mark."""

    def __init__(
        self, ws: WebSocketLike, *, frame_ms: int, lead_frames: int
    ) -> None:
        self._ws = ws
        self._frame_s = frame_ms / 1000.0
        # Bounded so the producer (TTS) blocks rather than flooding Telnyx: the
        # queue depth is the lead Telnyx buffers ahead of the pacer.
        self._out: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=lead_frames)
        self._pacer_task: asyncio.Task[None] | None = None
        self._epoch = 0
        self._closed = False
        # Set by the server to the agent's wind-down hook; invoked if the
        # outbound socket dies so the call tears down instead of going mute.
        self.on_error: Callable[[], None] | None = None

    def start(self) -> None:
        """Start the pacer task. Called once the stream is live."""
        self._pacer_task = asyncio.create_task(self._pacer())

    def begin_utterance(self) -> int:
        """Return the epoch a new spoken turn should tag its frames with."""
        return self._epoch

    async def send_audio_frame(self, epoch: int, frame: bytes) -> None:
        """Enqueue one frame for paced injection; blocks while the lead is full.

        The epoch is checked before and carried with the frame, so a frame for an
        utterance that flush() has since invalidated is never injected.
        """
        if self._closed or epoch != self._epoch:
            return
        await self._out.put((epoch, _AUDIO, frame))

    async def send_mark(self, epoch: int, name: str) -> None:
        """Queue a mark fence; Telnyx echoes it once the preceding audio plays."""
        if self._closed or epoch != self._epoch:
            return
        await self._out.put((epoch, _MARK, name.encode("utf-8")))

    async def flush(self) -> None:
        """Drop queued outbound audio and stop Telnyx playback immediately.

        Bumps the epoch so frames from the interrupted utterance still queued (or
        enqueued by a producer blocked mid-flush) are dropped, clears the local
        queue, then sends the Telnyx clear event. This is the barge-in primitive.
        """
        self._epoch += 1
        self._drain()
        with contextlib.suppress(Exception):
            await self._ws.send_text(json.dumps({"event": "clear"}))
        log.info("media.flushed", epoch=self._epoch)

    def _drain(self) -> None:
        while True:
            try:
                self._out.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _fail(self) -> None:
        """Outbound socket died: stop, release the blocked producer, notify.

        Bumping the epoch and draining frees a send_audio_frame blocked on the
        full queue at once; on_error winds the call down (like the STT client's
        reconnect-exhausted path) rather than leaving it mute for the rest of the
        call.
        """
        self._closed = True
        self._epoch += 1
        self._drain()
        if self.on_error is not None:
            self.on_error()

    async def _pacer(self) -> None:
        """Send one queued frame per frame_ms; marks pass through immediately."""
        while True:
            epoch, kind, payload = await self._out.get()
            if epoch != self._epoch:
                continue  # stale: belongs to a flushed utterance
            if kind == _AUDIO:
                encoded = encode_l16_payload(payload)
                msg = json.dumps({"event": "media", "media": {"payload": encoded}})
                try:
                    await self._ws.send_text(msg)
                except Exception as e:
                    log.warning("media.send_failed", error=str(e))
                    self._fail()
                    return
                await asyncio.sleep(self._frame_s)
            else:  # mark fence, sent after the preceding audio has drained
                msg = json.dumps(
                    {"event": "mark", "mark": {"name": payload.decode("utf-8")}}
                )
                with contextlib.suppress(Exception):
                    await self._ws.send_text(msg)

    async def aclose(self) -> None:
        """Stop the pacer and drop any queued audio. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._epoch += 1
        self._drain()
        if self._pacer_task is not None:
            self._pacer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pacer_task
        log.info("media.closed")
