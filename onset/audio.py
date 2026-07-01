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
    import queue
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


# ── Streaming MP3 decode ─────────────────────────────────────────
#
# The whole-buffer decode_mp3_to_pcm16 above waits for the entire MP3 before it
# can emit anything. For low time-to-first-audio the TTS client instead feeds
# MP3 to miniaudio incrementally as it arrives off the socket and emits PCM16
# frames as they decode. miniaudio's streaming decoder (stream_any) is a
# synchronous, pull-based generator: it calls QueueStreamSource.read() for more
# encoded bytes and yields decoded PCM. The same decoder and the same
# sample_rate/mono settings as the whole-buffer path, so the streamed output
# matches it (this is what the offline validation gate byte-compares before any
# live call).


class Pcm16Reframer:
    """Reslices a stream of PCM16 bytes into fixed-size frames.

    stream_mp3_frames yields variable-size decoded chunks; the media path wants
    exact frame_bytes frames. push() returns every whole frame it can carve from
    what has arrived so far, holding the remainder; flush() returns the trailing
    partial frame zero-padded (so the last syllable is not clipped), matching
    frame_pcm16's padding rule.
    """

    def __init__(self, frame_bytes: int) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        self._frame_bytes = frame_bytes
        self._buf = bytearray()

    def push(self, pcm16: bytes) -> list[bytes]:
        """Append PCM and return any whole frames now available."""
        self._buf += pcm16
        n = self._frame_bytes
        frames: list[bytes] = []
        while len(self._buf) >= n:
            frames.append(bytes(self._buf[:n]))
            del self._buf[:n]
        return frames

    def flush(self) -> bytes | None:
        """Return the trailing partial frame zero-padded, or None if aligned."""
        if not self._buf:
            return None
        frame = bytes(self._buf) + b"\x00" * (self._frame_bytes - len(self._buf))
        self._buf.clear()
        return frame


class QueueStreamSource(miniaudio.StreamableSource):  # type: ignore[misc]
    """A miniaudio streaming source fed encoded bytes through a thread-safe queue.

    The async WS recv loop puts MP3 chunks on the queue (and None to mark the end
    of the stream); the decoder thread pulls through read(). read() blocks while
    the queue is empty and the stream is not finished, because miniaudio treats a
    short read as success (it asks again) but an empty return as end-of-stream, so
    returning b"" early would truncate the audio. Partial reads are fine, so the
    decoder advances as soon as the first chunk lands.
    """

    def __init__(self, feed: queue.Queue[bytes | None]) -> None:
        self._feed = feed
        self._buf = bytearray()
        self._eof = False

    def read(self, num_bytes: int) -> bytes:
        while not self._buf and not self._eof:
            item = self._feed.get()
            if item is None:
                self._eof = True
            else:
                self._buf += item
        if not self._buf:
            return b""
        take = bytes(self._buf[:num_bytes])
        del self._buf[:num_bytes]
        return take


def stream_mp3_frames(
    source: miniaudio.StreamableSource, *, sample_rate: int, frame_bytes: int
) -> Iterator[bytes]:
    """Decode an MP3 source incrementally, yielding frame_bytes PCM16 frames.

    miniaudio resamples to sample_rate and downmixes to mono in the same pass as
    decode_mp3_to_pcm16, so this is the same single transcode, just streamed.
    frames_to_read is one output frame so the first frame is emitted after the
    first MP3 frame decodes rather than after a large buffer fills. The final
    partial frame is zero-padded.
    """
    reframer = Pcm16Reframer(frame_bytes)
    stream = miniaudio.stream_any(
        source,
        source_format=miniaudio.FileFormat.MP3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=sample_rate,
        frames_to_read=frame_bytes // 2,
    )
    for chunk in stream:
        yield from reframer.push(chunk.tobytes())
    tail = reframer.flush()
    if tail is not None:
        yield tail
