"""Offline validation gate for the streaming TTS decoder (run before any live call).

The streaming decode path (onset/audio.stream_mp3_frames + QueueStreamSource) must
produce the SAME audio as the proven whole-buffer decode (decode_mp3_to_pcm16)
before it is trusted on a real call. This script pulls one real TTS MP3 off the
socket, records it in the WS-sized chunks Telnyx actually sends, then:

  1. decodes the whole buffer (the reference path, miniaudio.decode),
  2. feeds the SAME chunks through the streaming decoder (miniaudio.stream_any),
  3. compares the two, and
  4. measures the first-audio head start: when the streaming path can emit its
     first frame vs when the whole-buffer path can (only after isFinal).

Known benign delta: miniaudio's whole-buffer decode emits the MP3 decoder priming
samples (a fixed run of leading silence, the codec delay) that its streaming
decoder strips. So the two are identical after a small leading-silence offset. The
gate accounts for that explicitly: it aligns on the offset, PROVES the leading
region is silence, and only then passes. Any other delta fails.

Outbound only (we dial Telnyx), so no public endpoint is needed. Reads
TELNYX_API_KEY from the environment. Makes one billable TTS call.

Run:
  export TELNYX_API_KEY=...        # not echoed
  .venv/bin/python scripts/validate_streaming_decode.py
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import os
import queue
import sys
import time
import wave
from dataclasses import dataclass, field

import miniaudio
from websockets.asyncio.client import connect as ws_connect

from onset.audio import QueueStreamSource, decode_mp3_to_pcm16

API_KEY = os.environ.get("TELNYX_API_KEY", "").strip()
BASE = "wss://api.telnyx.com/v2"
VOICE = "Telnyx.NaturalHD.astra"
SAMPLE_RATE = 16000
FRAME_BYTES = 640  # 20 ms of mono PCM16 at 16 kHz; matches Settings.frame_bytes
# A leading-silence offset up to this peak amplitude counts as codec priming, not
# clipped audio (PCM16 full scale is +/-32768; this is roughly -48 dBFS).
SILENCE_PEAK = 128
# A multi-sentence reply so the difference between streaming and whole-buffer
# (which waits for the whole real-time MP3) is meaningful.
UTTERANCE = (
    "Thanks for calling. I can help you book a table, change an existing "
    "reservation, or answer questions about the menu. What would you like to do "
    "today? If you already have a booking, please have the name it is under ready."
)


@dataclass
class Recording:
    """The MP3 reply, captured as the chunks Telnyx sent and when they arrived."""

    chunks: list[bytes] = field(default_factory=list)
    arrivals: list[float] = field(default_factory=list)  # seconds since send
    final_at: float | None = None  # when isFinal arrived


async def record_tts() -> Recording:
    """Connect to the TTS socket and record the MP3 reply chunk by chunk."""
    url = f"{BASE}/text-to-speech/speech?voice={VOICE}"
    rec = Recording()
    async with ws_connect(
        url,
        additional_headers={"Authorization": f"Bearer {API_KEY}"},
        open_timeout=15,
        max_size=None,
    ) as ws:
        t0 = time.monotonic()
        await ws.send(json.dumps({"text": " "}))
        await ws.send(json.dumps({"text": UTTERANCE}))
        await ws.send(json.dumps({"text": ""}))
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
            except TimeoutError:
                print("  [recv timeout]")
                break
            now = time.monotonic() - t0
            if isinstance(msg, (bytes, bytearray)):
                rec.chunks.append(bytes(msg))
                rec.arrivals.append(now)
                continue
            data = json.loads(msg)
            audio = data.get("audio")
            if audio:
                rec.chunks.append(base64.b64decode(audio))
                rec.arrivals.append(now)
            if data.get("isFinal") is True:
                rec.final_at = now
                break
            if data.get("error") or data.get("errors"):
                print(f"  TTS error: {json.dumps(data)[:300]}")
                break
    return rec


def streamed_pcm(chunks: list[bytes]) -> bytes:
    """Decode the recorded chunks through the production streaming decoder path.

    Same source and miniaudio call as onset.audio.stream_mp3_frames (the reframing
    it adds on top is deterministic and unit-tested separately); this compares the
    decode itself against the whole-buffer decode.
    """
    feed: queue.Queue[bytes | None] = queue.Queue()
    for chunk in chunks:
        feed.put(chunk)
    feed.put(None)  # end-of-stream
    source = QueueStreamSource(feed)
    stream = miniaudio.stream_any(
        source,
        source_format=miniaudio.FileFormat.MP3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=SAMPLE_RATE,
        frames_to_read=FRAME_BYTES // 2,
    )
    out = bytearray()
    for chunk in stream:
        out += chunk.tobytes()
    return bytes(out)


def compare(reference: bytes, streamed: bytes, sample_rate: int) -> bool:
    """Prove streamed == reference, exactly or after a leading-silence offset."""
    print(f"\n{'=' * 72}\nDECODE COMPARE")
    print(f"  reference bytes={len(reference)}  streamed bytes={len(streamed)}")
    if reference == streamed:
        print("  RESULT: exact match (streaming decode == whole-buffer decode)")
        return True

    ref = array.array("h")
    ref.frombytes(reference)
    st = array.array("h")
    st.frombytes(streamed)

    # miniaudio's whole-buffer decode carries the MP3 decoder priming samples that
    # its streaming decode strips, so the reference leads by that fixed count.
    offset = len(ref) - len(st)
    aligned = offset >= 0 and ref[offset:] == st
    if aligned:
        lead = ref[:offset]
        lead_peak = max((abs(x) for x in lead), default=0)
        ms = 1000 * offset / sample_rate
        print(f"  aligned after a {offset}-sample ({ms:.1f} ms) leading offset")
        print(f"  leading region peak amplitude={lead_peak} (PCM16 +/-32768)")
        if lead_peak <= SILENCE_PEAK:
            print("  RESULT: match after inaudible leading-silence codec priming")
            return True
        print("  RESULT: MISMATCH - leading region is not silence (audio clipped)")
        return False

    # Not a clean offset: characterize the delta so a human can judge.
    n = min(len(ref), len(st))
    first_diff = next((i for i in range(n) if ref[i] != st[i]), n)
    diff_count = sum(1 for i in range(n) if ref[i] != st[i])
    max_abs = max((abs(ref[i] - st[i]) for i in range(n)), default=0)
    print("  RESULT: MISMATCH")
    print(f"  sample-length delta={len(st) - len(ref)}")
    print(f"  first differing sample={first_diff} (of {n})")
    print(f"  differing samples in overlap={diff_count}")
    print(f"  max abs sample difference={max_abs} (PCM16 +/-32768)")
    return False


def first_audio_chunk(rec: Recording) -> tuple[int, float] | None:
    """Smallest chunk prefix that decodes to at least one frame, and its arrival.

    This is when the streaming path could emit its first frame: as soon as enough
    MP3 has arrived to decode one frame, not after the whole reply.
    """
    for k in range(1, len(rec.chunks) + 1):
        prefix = b"".join(rec.chunks[:k])
        try:
            pcm = decode_mp3_to_pcm16(prefix, SAMPLE_RATE)
        except Exception:  # noqa: BLE001 - a short prefix may not decode yet
            pcm = b""
        if len(pcm) >= FRAME_BYTES:
            return k, rec.arrivals[k - 1]
    return None


def write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


async def main() -> int:
    if not API_KEY:
        print("TELNYX_API_KEY not set", file=sys.stderr)
        return 2
    print(f"validating streaming decode as key ...{API_KEY[-4:]}")
    rec = await record_tts()
    if not rec.chunks:
        print("no audio received", file=sys.stderr)
        return 2

    full_mp3 = b"".join(rec.chunks)
    print(
        f"\nrecorded: chunks={len(rec.chunks)} mp3_bytes={len(full_mp3)} "
        f"first_chunk_at={rec.arrivals[0]:.3f}s final_at={rec.final_at}"
    )

    t = time.monotonic()
    reference = decode_mp3_to_pcm16(full_mp3, SAMPLE_RATE)
    decode_ms = round((time.monotonic() - t) * 1000)

    t = time.monotonic()
    streamed = streamed_pcm(rec.chunks)
    stream_ms = round((time.monotonic() - t) * 1000)

    ok = compare(reference, streamed, SAMPLE_RATE)

    print(f"\n{'=' * 72}\nFIRST-AUDIO HEAD START")
    fa = first_audio_chunk(rec)
    whole_buffer_first = (rec.final_at or rec.arrivals[-1]) + decode_ms / 1000
    print(f"  whole-buffer path: first frame at ~{whole_buffer_first:.3f}s")
    print(f"    (waits for isFinal at {rec.final_at}s, then {decode_ms} ms decode)")
    if fa is not None:
        k, at = fa
        print(f"  streaming path:    first frame at ~{at:.3f}s")
        print(f"    (decodes after chunk {k} of {len(rec.chunks)} arrives)")
        print(f"  head start: ~{whole_buffer_first - at:.3f}s sooner")
    else:
        print("  streaming path: no prefix decoded a full frame (investigate)")
    print(f"  (full streamed decode wall time {stream_ms} ms, decode {decode_ms} ms)")

    write_wav("out_reference.wav", reference)
    write_wav("out_streamed.wav", streamed)
    print("\nwrote out_reference.wav and out_streamed.wav for a listen")

    if not ok:
        print("\nGATE FAILED: streamed decode does not match whole-buffer decode")
        return 1
    print("\nGATE PASSED: safe to enable tts_streaming_decode and do a live call")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
