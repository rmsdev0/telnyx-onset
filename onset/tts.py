"""Telnyx Text-to-Speech WebSocket client.

Text in, MP3 out: the socket streams base64 MP3 chunks with isFinal on the last
one. The media socket needs raw L16, so the MP3 is decoded to PCM16 (the one
transcode in the loop) off the event loop and sliced into frames the agent paces
into the call.

A fresh socket is opened per synthesized turn so cancellation is clean: when
barge-in cancels the response task, the generator's finally closes the socket and
the partial synthesis is abandoned with no cross-turn state. Whole-turn synthesis
keeps playback gap-free; streaming sentence-by-sentence is a latency follow-on.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import structlog
from websockets.asyncio.client import connect as ws_connect

from onset.audio import decode_mp3_to_pcm16, frame_pcm16

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from onset.settings import Settings

log = structlog.get_logger()

# Cap a single recv so a missing isFinal cannot hang the turn.
_RECV_TIMEOUT_S = 20.0


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

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize text and yield frame-aligned PCM16 at the configured rate.

        Opens a TTS socket, streams the text, collects the MP3 response, decodes
        it to PCM16, and yields frames. Closes the socket on exit, including when
        the consumer is cancelled by barge-in.
        """
        text = text.strip()
        if not text:
            return
        s = self._settings
        mp3 = bytearray()
        ws = await ws_connect(
            self._url(),
            additional_headers={"Authorization": f"Bearer {s.telnyx_api_key}"},
            open_timeout=10,
            max_size=None,
        )
        try:
            await ws.send(json.dumps({"text": " "}))
            await ws.send(json.dumps({"text": text}))
            await ws.send(json.dumps({"text": ""}))
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
