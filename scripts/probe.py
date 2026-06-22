"""Live probe of the Telnyx STT and TTS WebSockets (build step 0).

Resolves the residual format unknowns from DISCOVERY.md before the loop is wired:
  1. TTS WS: output codec, message framing, first-audio latency. (RESOLVED: MP3.)
  2. STT WS: which engine / encoding / sample_rate / input_format is accepted, what
     raw audio framing works, and what transcript / turn-taking signals come back
     (especially Deepgram Flux end-of-turn).

Outbound only: we connect to Telnyx, so no public endpoint is needed.

Reads TELNYX_API_KEY from the environment. Makes a few billable calls.

Run:
  export TELNYX_API_KEY=...        # not echoed
  .venv/bin/python scripts/probe.py
"""

from __future__ import annotations

import asyncio
import audioop  # available on 3.12 (removed in 3.13; only used by this probe)
import base64
import contextlib
import io
import json
import os
import time
import wave
from typing import Any

from websockets.asyncio.client import connect as ws_connect

API_KEY = os.environ.get("TELNYX_API_KEY", "").strip()
BASE = "wss://api.telnyx.com/v2"
UTTERANCE = "I would like to book a table for four people, tomorrow at seven."


def sniff_header(b: bytes) -> str:
    if b[:3] == b"ID3":
        return "MP3 (ID3 tag)"
    if len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0:
        return "MP3 (MPEG frame sync)"
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "WAV / RIFF"
    if b[:4] == b"OggS":
        return "OGG"
    return f"raw / unknown (first 12 bytes hex: {b[:12].hex()})"


def connect(url: str) -> Any:
    return ws_connect(
        url,
        additional_headers={"Authorization": f"Bearer {API_KEY}"},
        open_timeout=15,
        max_size=None,
    )


def explain_exc(e: BaseException) -> str:
    resp = getattr(e, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", "?")
        body = getattr(resp, "body", b"") or b""
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body)[:300].decode("utf-8", "replace")
        return f"{type(e).__name__}: HTTP {status} body={body!r}"
    return repr(e)


async def probe_tts() -> bytes:
    url = f"{BASE}/text-to-speech/speech?voice=Telnyx.NaturalHD.astra"
    print(f"\n{'=' * 72}\nTTS  {url}")
    audio = bytearray()
    keys_seen: set[str] = set()
    first_latency: float | None = None
    n_audio = 0
    try:
        async with connect(url) as ws:
            t0 = time.monotonic()
            await ws.send(json.dumps({"text": " "}))
            await ws.send(json.dumps({"text": UTTERANCE}))
            await ws.send(json.dumps({"text": ""}))
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=12)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    if first_latency is None:
                        first_latency = time.monotonic() - t0
                    audio += msg
                    n_audio += 1
                    continue
                data = json.loads(msg)
                keys_seen |= set(data.keys())
                if data.get("audio"):
                    if first_latency is None:
                        first_latency = time.monotonic() - t0
                    audio += base64.b64decode(data["audio"])
                    n_audio += 1
                if data.get("isFinal") is True:
                    break
                if "error" in data:
                    print(f"  TTS error: {json.dumps(data)[:300]}")
                    break
    except Exception as e:
        print(f"  TTS exception: {explain_exc(e)}")
    print(f"  keys={sorted(keys_seen)}  audio_msgs={n_audio}  bytes={len(audio)}")
    print(f"  first-audio latency={first_latency}  header={sniff_header(bytes(audio)) if audio else 'n/a'}")
    return bytes(audio)


def decode_to_pcm16_16k(audio: bytes) -> bytes | None:
    try:
        import miniaudio

        dsf = miniaudio.decode(
            audio,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=16000,
        )
    except Exception as e:
        print(f"  [miniaudio decode failed: {e!r}]")
        return None
    pcm = dsf.samples.tobytes()
    print(f"  decoded TTS MP3 -> PCM16/16k/mono: {len(pcm)} bytes ({dsf.num_frames / 16000:.2f}s)")
    return pcm


def make_payloads(pcm16_16k: bytes, mp3_bytes: bytes) -> dict[str, tuple[bytes, int]]:
    """name -> (bytes, frame_size). frame_size ~ 20 ms where it matters."""
    pcm8k, _ = audioop.ratecv(pcm16_16k, 2, 1, 16000, 8000, None)
    mulaw8k = audioop.lin2ulaw(pcm8k, 2)
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm16_16k)
    return {
        "linear16_16k": (pcm16_16k, 640),
        "linear16_8k": (pcm8k, 320),
        "mulaw_8k": (mulaw8k, 160),
        "wav_16k": (wav_buf.getvalue(), 1024),
        "mp3": (mp3_bytes, 4096),
    }


async def try_stt(name: str, url: str, payload: bytes, frame: int, pace: float, raw: bool) -> bool:
    print(f"\n{'=' * 72}\nSTT  [{name}]\n     {url}")
    msgs: list[tuple[float, Any]] = []
    got_transcript = False
    close_info = "?"
    t0 = time.monotonic()
    try:
        async with connect(url) as ws:

            async def feeder() -> None:
                with contextlib.suppress(Exception):
                    for i in range(0, len(payload), frame):
                        await ws.send(payload[i : i + frame])
                        if pace:
                            await asyncio.sleep(pace)
                    # trailing silence (raw encodings) to trip endpointing
                    if raw:
                        sil = (b"\xff" if "mulaw" in name else b"\x00") * frame
                        for _ in range(50):
                            await ws.send(sil)
                            await asyncio.sleep(0.02)

            feed = asyncio.create_task(feeder())
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    if feed.done():
                        break
                    continue
                except Exception:
                    break
                dt = time.monotonic() - t0
                if isinstance(msg, (bytes, bytearray)):
                    msgs.append((dt, {"<binary>": len(msg)}))
                    continue
                data = json.loads(msg)
                msgs.append((dt, data))
                if data.get("transcript"):
                    got_transcript = True
                if len(msgs) >= 60:
                    break
            feed.cancel()
            with contextlib.suppress(BaseException):
                await feed
        close_info = f"code={ws.close_code} reason={ws.close_reason!r}"
    except Exception as e:
        print(f"  handshake/exception: {explain_exc(e)}")
        return False

    union: set[str] = set()
    for _, m in msgs:
        if isinstance(m, dict):
            union |= set(m.keys())
    print(f"  close: {close_info}")
    print(f"  messages={len(msgs)}  union_keys={sorted(union)}  transcript={got_transcript}")
    for dt, m in msgs[:20]:
        body = json.dumps(m)
        # Print enumeration errors in full; they leak the supported value lists.
        limit = 600 if (isinstance(m, dict) and "errors" in m) else 230
        print(f"    +{dt:5.2f}s  {body[:limit]}")
    return got_transcript


async def probe_stt(pcm16_16k: bytes, mp3_bytes: bytes) -> str | None:
    pl = make_payloads(pcm16_16k, mp3_bytes)
    common = "language=en&interim_results=true"
    stt = f"{BASE}/speech-to-text/transcription"
    # The real loop streams RAW frames, so the raw candidates are what matter.
    # (name, url, payload_key, pace_seconds, raw)
    candidates = [
        ("CONFIRM input_format=linear16 16k raw @20ms", f"{stt}?transcription_engine=Deepgram&input_format=linear16&sample_rate=16000&{common}", "linear16_16k", 0.02, True),
        ("enumerate engines (bad value)", f"{stt}?transcription_engine=__list__&input_format=mp3&{common}", "mp3", 0.05, False),
        ("enumerate models (bad value)", f"{stt}?transcription_engine=Deepgram&model=__list__&input_format=linear16&sample_rate=16000&{common}", "linear16_16k", 0.02, True),
        ("flux via transcription_model", f"{stt}?transcription_engine=Deepgram&transcription_model=flux-general-en&input_format=linear16&sample_rate=16000&{common}", "linear16_16k", 0.02, True),
        ("flux engine token deepgram/flux", f"{stt}?transcription_engine=deepgram/flux&input_format=linear16&sample_rate=16000&{common}", "linear16_16k", 0.02, True),
    ]
    working: list[str] = []
    for name, url, key, pace, raw in candidates:
        data, frame = pl[key]
        try:
            ok = await try_stt(name, url, data, frame, pace, raw)
        except Exception as e:
            print(f"  [candidate raised: {explain_exc(e)}]")
            ok = False
        if ok:
            working.append(name)
    return ", ".join(working) if working else None


async def main() -> None:
    if not API_KEY:
        raise SystemExit("TELNYX_API_KEY not set")
    print(f"probing as key ...{API_KEY[-4:]} (len {len(API_KEY)})")
    mp3 = await probe_tts()
    pcm = decode_to_pcm16_16k(mp3) if mp3 else None
    if pcm:
        working = await probe_stt(pcm, mp3)
        print(f"\n{'=' * 72}\nWORKING STT CONFIG: {working}")
    else:
        print("[skip STT: no PCM]")


if __name__ == "__main__":
    asyncio.run(main())
