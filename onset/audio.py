"""Audio helpers for the L16 / 16 kHz media path.

The media socket, the STT socket, and (after decode) the TTS output all run at
one rate, mono PCM16 at 16 kHz, so there is no resampling and no mu-law anywhere
in app code. The only transcode in the loop is decoding the TTS socket's MP3
output to PCM16, done here with miniaudio (self-contained wheels, no system
ffmpeg). Inbound L16 frames arrive base64-encoded with no RTP headers, so
decoding them is a plain base64 decode.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import miniaudio

if TYPE_CHECKING:
    from collections.abc import Iterator


def decode_l16_payload(payload: str) -> bytes:
    """Decode a base64 media payload to raw little-endian PCM16 bytes.

    Telnyx delivers the L16 RTP payload base64-encoded with no RTP headers, so
    this is a plain base64 decode: the result is the raw 16-bit PCM samples.
    """
    return base64.b64decode(payload)


def encode_l16_payload(pcm16: bytes) -> str:
    """Encode raw PCM16 bytes as the base64 payload for outbound injection."""
    return base64.b64encode(pcm16).decode("ascii")


def frame_pcm16(pcm16: bytes, frame_bytes: int) -> Iterator[bytes]:
    """Slice PCM16 into fixed-size frames, padding a trailing partial frame.

    A short final frame is padded with silence so the injected audio stays
    frame-aligned and the last syllable is not clipped.
    """
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    for i in range(0, len(pcm16), frame_bytes):
        frame = pcm16[i : i + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + b"\x00" * (frame_bytes - len(frame))
        yield frame


def decode_mp3_to_pcm16(mp3: bytes, sample_rate: int) -> bytes:
    """Decode an MP3 byte stream to mono little-endian PCM16 at sample_rate.

    The Telnyx TTS socket returns MP3 only; this is the single transcode in the
    loop. miniaudio resamples to sample_rate and downmixes to mono in one pass.
    Returns empty bytes for empty or undecodable input.
    """
    if not mp3:
        return b""
    decoded = miniaudio.decode(
        mp3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=sample_rate,
    )
    samples: bytes = decoded.samples.tobytes()
    return samples
