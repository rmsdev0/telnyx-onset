"""Telnyx Speech-to-Text WebSocket client (Deepgram via Telnyx, Wiring X).

The app pipes the inbound caller frames it already owns into this socket and
receives transcripts back: one Telnyx API key, no call binding. input_format=
linear16 takes the media socket's L16 audio with no transcode. The transcript
schema is {transcript, is_final, speech_final}: speech_final marks end of
utterance and drives turn-taking, while barge-in is the local VAD.

Inbound audio is fed through a bounded drop-oldest queue, so a slow or stalled
socket cannot grow memory without bound; dropping the oldest frames degrades
transcription gracefully rather than ballooning latency. A bounded reconnect
keeps a transient drop from killing the call, and exhausting it signals the
agent so the call can wind down cleanly rather than going deaf.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import structlog
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

if TYPE_CHECKING:
    from onset.settings import Settings

log = structlog.get_logger()

# (transcript, is_final, speech_final) -> None
TranscriptCallback = Callable[[str, bool, bool], None]


class SttClient:
    """A streaming STT socket scoped to one call."""

    def __init__(
        self,
        settings: Settings,
        *,
        on_transcript: TranscriptCallback,
        on_error: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._on_transcript = on_transcript
        self._on_error = on_error
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._send_task: asyncio.Task[None] | None = None
        self._feed: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=settings.stt_feed_max_frames
        )
        self._closed = False
        self._fatal = False

    def _url(self) -> str:
        s = self._settings
        query = urlencode(
            {
                "transcription_engine": s.stt_engine,
                "input_format": s.stt_input_format,
                "sample_rate": s.sample_rate,
                "language": s.stt_language,
                "interim_results": "true",
            }
        )
        return f"{s.telnyx_ws_base}/speech-to-text/transcription?{query}"

    async def _open(self) -> ClientConnection:
        return await ws_connect(
            self._url(),
            additional_headers={
                "Authorization": f"Bearer {self._settings.telnyx_api_key}"
            },
            open_timeout=10,
            max_size=None,
        )

    async def connect(self) -> None:
        """Open the socket and start the send and receive tasks."""
        if self._closed:
            return
        self._ws = await self._open()
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._send_task = asyncio.create_task(self._send_loop())
        log.info("stt.connected", engine=self._settings.stt_engine)

    def feed(self, pcm16: bytes) -> None:
        """Enqueue inbound audio for the STT socket; drop-oldest when full."""
        if self._closed:
            return
        try:
            self._feed.put_nowait(pcm16)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._feed.get_nowait()  # drop the oldest frame
            with contextlib.suppress(asyncio.QueueFull):
                self._feed.put_nowait(pcm16)

    async def _send_loop(self) -> None:
        while not self._closed:
            pcm = await self._feed.get()
            ws = self._ws
            if ws is None:
                continue  # mid-reconnect: drop, the receive loop drives recovery
            try:
                await ws.send(pcm)
            except Exception as e:
                log.debug("stt.send_failed", error=str(e))

    async def _receive_loop(self) -> None:
        attempt = 0
        while not self._closed and not self._fatal:
            ws = self._ws
            # A null socket means a previous reopen failed; do not break here, or
            # the reconnect budget is bypassed and on_error never fires. Fall
            # through to the exhaustion check and the next reconnect attempt.
            if ws is not None:
                try:
                    async for raw in ws:
                        attempt = 0
                        self._handle_message(raw)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("stt.recv_error", error=str(e))
            if self._closed or self._fatal:
                break
            if attempt >= self._settings.stt_max_reconnects:
                log.error("stt.reconnect_exhausted")
                if self._on_error is not None:
                    self._on_error()
                break
            attempt += 1
            await asyncio.sleep(self._settings.stt_reconnect_backoff_s * attempt)
            try:
                self._ws = await self._open()
                log.info("stt.reconnected", attempt=attempt)
            except Exception as e:
                log.warning("stt.reconnect_failed", attempt=attempt, error=str(e))
                self._ws = None

    def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, (bytes, bytearray)):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if data.get("errors"):
            log.error("stt.provider_error", errors=data.get("errors"))
            return
        transcript = data.get("transcript")
        is_final = bool(data.get("is_final"))
        speech_final = bool(data.get("speech_final"))
        # The end-of-utterance frame can arrive empty ({"transcript": "",
        # "speech_final": true}); it still must drive the turn end, so only drop a
        # frame that is BOTH empty and not a speech_final endpoint.
        if not transcript and not speech_final:
            return
        self._on_transcript(str(transcript or ""), is_final, speech_final)

    async def aclose(self) -> None:
        """Cancel the send and receive tasks and close the socket. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for task in (self._recv_task, self._send_task):
            if task is not None:
                task.cancel()
        for task in (self._recv_task, self._send_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        log.info("stt.closed")
