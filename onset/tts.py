"""Telnyx Text-to-Speech WebSocket client.

Text in, MP3 out: the socket streams base64 MP3 chunks with isFinal on the last
one. The media socket needs raw L16, so the MP3 is decoded to PCM16 (the one
transcode in the loop) and sliced into frames the agent paces into the call.

A fresh socket is opened per synthesized turn so cancellation is clean: when
barge-in cancels the response task, the generator's finally closes the socket and
the partial synthesis is abandoned with no cross-turn state.

Two decode paths, selected by settings.tts_streaming_decode:

- Buffered (default): collect the whole MP3, decode it in one off-loop pass, then
  yield frames. Simple and proven, but first audio waits for the whole reply.
- Streaming: decode the MP3 incrementally as chunks arrive and yield frames as
  they decode, so first audio leaves about one MP3 chunk after synthesis starts.
  miniaudio's streaming decoder is synchronous and pull-based, so it runs in a
  worker thread fed by the async recv loop through a queue, and hands finished
  frames back to the loop through another. The outbound seam is identical: both
  paths yield the same frame_bytes PCM16 frames.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import structlog
from websockets.asyncio.client import connect as ws_connect

from onset.audio import (
    QueueStreamSource,
    decode_mp3_to_pcm16,
    frame_pcm16,
    stream_mp3_frames,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from websockets.asyncio.client import ClientConnection

    from onset.settings import Settings

log = structlog.get_logger()

# Cap a single recv so a missing isFinal cannot hang the turn.
_RECV_TIMEOUT_S = 20.0


@dataclass(frozen=True, slots=True)
class _Done:
    """End-of-stream marker the decoder thread puts on the output queue.

    error carries a decode failure back to the loop so the consumer can decide
    whether to surface it (no audio emitted yet) or end quietly (some already
    played); None on a clean finish.
    """

    error: BaseException | None


def _decode_loop(
    loop: asyncio.AbstractEventLoop,
    feed_q: queue.Queue[bytes | None],
    out_q: asyncio.Queue[bytes | _Done],
    *,
    sample_rate: int,
    frame_bytes: int,
) -> None:
    """Drive the synchronous streaming decoder; runs in a worker thread.

    Pulls MP3 from feed_q (via the source), decodes and reframes to PCM16, and
    hands each frame to the event loop through out_q. call_soon_threadsafe is the
    thread-safe way to push onto an asyncio.Queue; it raises only once the loop is
    gone, at which point the turn is being torn down and the frame is moot.
    """
    source = QueueStreamSource(feed_q)
    error: BaseException | None = None

    def emit(item: bytes | _Done) -> bool:
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, item)
        except RuntimeError:
            return False  # loop closed: nothing left to feed
        return True

    try:
        for frame in stream_mp3_frames(
            source, sample_rate=sample_rate, frame_bytes=frame_bytes
        ):
            if not emit(frame):
                return
    except Exception as exc:  # noqa: BLE001 - reported to the loop, not swallowed
        error = exc
    emit(_Done(error))


class TtsClient:
    """Synthesizes a turn's text into paced PCM16 frames over the TTS socket."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _url(self) -> str:
        s = self._settings
        return (
            f"{s.telnyx_ws_base}/text-to-speech/speech"
            f"?{urlencode({'voice': s.tts_voice})}"
        )

    async def _connect(self) -> ClientConnection:
        s = self._settings
        return await ws_connect(
            self._url(),
            additional_headers={"Authorization": f"Bearer {s.telnyx_api_key}"},
            open_timeout=10,
            max_size=None,
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize text and yield frame-aligned PCM16 at the configured rate.

        Opens a TTS socket, streams the text, decodes the MP3 response, and yields
        frames. Closes the socket on exit, including when the consumer is
        cancelled by barge-in. Streaming or buffered decode per settings.
        """
        text = text.strip()
        if not text:
            return
        if self._settings.tts_streaming_decode:
            inner = self._synthesize_streaming(text)
        else:
            inner = self._synthesize_buffered(text)
        # Close the delegate explicitly: when barge-in closes this generator, an
        # async for alone would leave the inner generator's finally (socket and
        # decoder teardown) to deferred finalization. aclose() runs it now.
        try:
            async for frame in inner:
                yield frame
        finally:
            await inner.aclose()

    async def _synthesize_buffered(self, text: str) -> AsyncGenerator[bytes, None]:
        """Collect the whole MP3, then decode and yield frames (whole-buffer)."""
        s = self._settings
        mp3 = bytearray()
        ws = await self._connect()
        try:
            await self._send_text(ws, text)
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT_S)
                if isinstance(raw, (bytes, bytearray)):
                    mp3 += raw
                    continue
                data = json.loads(raw)
                chunk = data.get("audio")
                if chunk:
                    mp3 += base64.b64decode(chunk)
                if data.get("isFinal") is True:
                    break
                if data.get("error") or data.get("errors"):
                    log.error("tts.provider_error", message=str(raw)[:300])
                    break
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

        if not mp3:
            return
        # MP3 decode is CPU work; keep it off the event loop so audio I/O on the
        # other sockets is not stalled.
        pcm = await asyncio.to_thread(decode_mp3_to_pcm16, bytes(mp3), s.sample_rate)
        for frame in frame_pcm16(pcm, s.frame_bytes):
            yield frame

    async def _synthesize_streaming(self, text: str) -> AsyncGenerator[bytes, None]:
        """Decode the MP3 incrementally and yield frames as they arrive.

        The recv pump (async) feeds MP3 chunks to the decoder thread through
        feed_q; the thread decodes and reframes and hands frames back through
        out_q; this generator yields them. The finally tears all of it down on
        normal completion and on barge-in cancellation: it unblocks the decoder
        (feed_q sentinel), cancels the pump, and closes the socket.
        """
        s = self._settings
        loop = asyncio.get_running_loop()
        feed_q: queue.Queue[bytes | None] = queue.Queue()
        out_q: asyncio.Queue[bytes | _Done] = asyncio.Queue()

        ws = await self._connect()
        pump: asyncio.Task[None] | None = None
        try:
            await self._send_text(ws, text)
            pump = asyncio.create_task(self._pump(ws, feed_q))
            decoder = threading.Thread(
                target=_decode_loop,
                args=(loop, feed_q, out_q),
                kwargs={"sample_rate": s.sample_rate, "frame_bytes": s.frame_bytes},
                daemon=True,
            )
            decoder.start()

            frames_emitted = 0
            while True:
                item = await out_q.get()
                if isinstance(item, _Done):
                    if item.error is not None and frames_emitted == 0:
                        raise item.error
                    if item.error is not None:
                        log.warning(
                            "tts.decode_failed_midstream",
                            error=str(item.error),
                            frames=frames_emitted,
                        )
                    return
                yield item
                frames_emitted += 1
        finally:
            # Unblock the decoder thread (sentinel), close the socket, then reap the
            # pump. Sentinel first so a read() blocked on an empty feed_q returns and
            # the thread exits. ws.close() must come BEFORE awaiting the pump: on the
            # barge-in path `await pump` re-raises CancelledError, which is a
            # BaseException, so a suppress(Exception) would not catch it and the
            # close would be skipped, leaking the socket until GC. Closing the socket
            # also makes the pump's recv() return, so the reap does not block.
            feed_q.put(None)
            if pump is not None:
                pump.cancel()
            with contextlib.suppress(Exception):
                await ws.close()
            if pump is not None:
                with contextlib.suppress(BaseException):
                    await pump

    async def _send_text(self, ws: ClientConnection, text: str) -> None:
        """Send the init / content / end frames the TTS socket expects."""
        await ws.send(json.dumps({"text": " "}))
        await ws.send(json.dumps({"text": text}))
        await ws.send(json.dumps({"text": ""}))

    async def _pump(
        self, ws: ClientConnection, feed_q: queue.Queue[bytes | None]
    ) -> None:
        """Recv MP3 chunks into feed_q until the reply ends, then mark the end.

        Keeps the per-recv timeout so a missing isFinal fails the turn fast rather
        than hanging. The finally always marks end-of-stream (None) so the decoder
        thread never blocks forever, including on cancellation during teardown.

        Records a delivery profile (tts.stream_profile): how quickly the first
        chunk arrives, over what span the chunks stream, and the largest gap
        between them. That is the jitter the pacer's playback buffer must absorb,
        so it is the ground truth for diagnosing playback smoothness.
        """
        t0 = time.monotonic()
        first_at: float | None = None
        last_at = 0.0
        prev_at = t0
        max_gap = 0.0
        n_chunks = 0
        n_bytes = 0

        def record(size: int) -> None:
            nonlocal first_at, last_at, prev_at, max_gap, n_chunks, n_bytes
            now = time.monotonic()
            if first_at is None:
                first_at = now - t0
            else:
                max_gap = max(max_gap, now - prev_at)
            prev_at = now
            last_at = now - t0
            n_chunks += 1
            n_bytes += size

        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT_S)
                if isinstance(raw, (bytes, bytearray)):
                    payload = bytes(raw)
                    feed_q.put(payload)
                    record(len(payload))
                    continue
                data = json.loads(raw)
                chunk = data.get("audio")
                if chunk:
                    decoded = base64.b64decode(chunk)
                    feed_q.put(decoded)
                    record(len(decoded))
                if data.get("isFinal") is True:
                    break
                if data.get("error") or data.get("errors"):
                    log.error("tts.provider_error", message=str(raw)[:300])
                    break
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("tts.recv_timeout")
        except Exception:
            log.warning("tts.recv_stopped", exc_info=True)
        finally:
            feed_q.put(None)
            if n_chunks:
                log.debug(
                    "tts.stream_profile",
                    chunks=n_chunks,
                    bytes=n_bytes,
                    first_chunk_ms=round((first_at or 0.0) * 1000),
                    span_ms=round((last_at - (first_at or 0.0)) * 1000),
                    max_gap_ms=round(max_gap * 1000),
                )
