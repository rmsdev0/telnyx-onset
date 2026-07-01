"""Streaming-decode helper tests: reframing and the queue-backed source."""

from __future__ import annotations

import queue

import pytest

from onset.audio import Pcm16Reframer, QueueStreamSource


def test_reframer_emits_whole_frames_and_buffers_remainder() -> None:
    r = Pcm16Reframer(frame_bytes=4)
    # Two and a half frames in: two whole frames out, half a frame held.
    assert r.push(b"AABBC") == [b"AABB"]
    assert r.push(b"CDD") == [b"CCDD"]
    assert r.push(b"") == []


def test_reframer_push_splits_a_large_chunk_into_many_frames() -> None:
    r = Pcm16Reframer(frame_bytes=2)
    assert r.push(b"123456") == [b"12", b"34", b"56"]


def test_reframer_flush_pads_trailing_partial_frame() -> None:
    r = Pcm16Reframer(frame_bytes=4)
    assert r.push(b"XY") == []
    assert r.flush() == b"XY\x00\x00"
    # Buffer is consumed by flush.
    assert r.flush() is None


def test_reframer_flush_none_when_aligned() -> None:
    r = Pcm16Reframer(frame_bytes=4)
    assert r.push(b"WXYZ") == [b"WXYZ"]
    assert r.flush() is None


def test_reframer_rejects_nonpositive_frame_bytes() -> None:
    with pytest.raises(ValueError, match="frame_bytes"):
        Pcm16Reframer(frame_bytes=0)


def test_queue_source_reads_chunks_then_eof() -> None:
    q: queue.Queue[bytes | None] = queue.Queue()
    for item in (b"MP3PART1", b"MP3PART2", None):
        q.put(item)
    src = QueueStreamSource(q)
    assert src.read(4096) == b"MP3PART1"
    assert src.read(4096) == b"MP3PART2"
    # Empty return marks end-of-stream to miniaudio.
    assert src.read(4096) == b""
    assert src.read(4096) == b""


def test_queue_source_returns_partial_within_a_chunk() -> None:
    q: queue.Queue[bytes | None] = queue.Queue()
    q.put(b"ABCDEF")
    q.put(None)
    src = QueueStreamSource(q)
    # A short read is fine (miniaudio asks again); the remainder is held.
    assert src.read(2) == b"AB"
    assert src.read(2) == b"CD"
    assert src.read(10) == b"EF"
    assert src.read(10) == b""
