"""Strict parser, authentication, ordering, and bounded-capture tests."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest

import bench.media_capture as media_capture
from bench.media_capture import (
    ArtifactDirectory,
    BoundedCapture,
    CaptureLimitError,
    ConnectedFrame,
    ErrorFrame,
    MarkFrame,
    MediaFormat,
    MediaFrame,
    ProbeProtocolError,
    StartFrame,
    StopFrame,
    StreamTokenStore,
    decode_probe_message,
    extract_stream_token,
    validate_authorized_call_id,
    validate_media_format,
)

if TYPE_CHECKING:
    from pathlib import Path


def media_raw(
    *, track: str = "inbound", sequence: int = 2, chunk: int = 1, timestamp: int = 20
) -> str:
    return json.dumps(
        {
            "event": "media",
            "sequence_number": str(sequence),
            "stream_id": "memory-only-stream",
            "media": {
                "track": track,
                "chunk": str(chunk),
                "timestamp": str(timestamp),
                "payload": base64.b64encode(b"\x01\x00" * 320).decode(),
            },
        }
    )


def test_one_use_token_validates_run_and_expires() -> None:
    store = StreamTokenStore(maximum=2)
    token = store.issue("run-one", "call-one", "probe", 10)
    authorization = store.consume(token, run_id="run-one", role="probe", now_ns=11)
    assert authorization.call_control_id == "call-one"
    assert store.pending == 0
    with pytest.raises(ProbeProtocolError, match="stream_auth_failed"):
        store.consume(token, run_id="run-one", role="probe", now_ns=12)

    expired = store.issue("run-one", "call-two", "probe", 20)
    with pytest.raises(ProbeProtocolError, match="stream_auth_failed"):
        store.consume(expired, run_id="run-one", role="probe", now_ns=60_000_000_021)


def test_token_wrong_run_missing_and_bounded_store() -> None:
    store = StreamTokenStore(maximum=1)
    token = store.issue("run-one", "call-one", "probe", 0)
    with pytest.raises(ProbeProtocolError, match="stream_auth_failed"):
        store.consume(token, run_id="run-two", role="probe", now_ns=1)
    with pytest.raises(ProbeProtocolError, match="stream_auth_failed"):
        store.consume(token, run_id="run-one", role="agent", now_ns=1)
    with pytest.raises(ProbeProtocolError, match="stream_auth_capacity"):
        store.issue("run-one", "call-two", "probe", 2)
    with pytest.raises(ProbeProtocolError, match="stream_auth_failed"):
        store.consume("", run_id="run-one", role="probe", now_ns=3)


def test_token_is_not_present_in_serialized_safe_state() -> None:
    store = StreamTokenStore()
    token = store.issue("run-one", "call-one", "probe", 0)
    safe = json.dumps({"pending_tokens": store.pending, "run": "run-one"})
    assert token not in safe


def test_connected_token_header_or_frame_extraction() -> None:
    connected = decode_probe_message(
        json.dumps(
            {
                "event": "connected",
                "version": "1.0.0",
                "x-telnyx-streaming-auth-token": "frame-token",
            }
        ),
        100,
    )
    assert isinstance(connected, ConnectedFrame)
    assert extract_stream_token({}, connected) == "frame-token"
    assert (
        extract_stream_token({"x-telnyx-streaming-auth-token": "header"}, connected)
        == "header"
    )
    with pytest.raises(ProbeProtocolError):
        extract_stream_token({}, None)


def test_start_and_media_preserve_required_metadata_for_both_tracks() -> None:
    start = decode_probe_message(
        json.dumps(
            {
                "event": "start",
                "sequence_number": "1",
                "stream_id": "memory-only-stream",
                "start": {
                    "call_control_id": "memory-only-call",
                    "media_format": {
                        "encoding": "L16",
                        "sample_rate": 16_000,
                        "channels": 1,
                    },
                },
            }
        ),
        101,
    )
    assert isinstance(start, StartFrame)
    assert start.sequence_number == 1
    assert start.media_format == MediaFormat("L16", 16_000, 1)
    validate_media_format(
        start.media_format, encoding="L16", sample_rate=16_000, channels=1
    )

    for track in ("inbound", "outbound"):
        media = decode_probe_message(media_raw(track=track), 102)
        assert isinstance(media, MediaFrame)
        assert (media.track, media.chunk, media.timestamp, media.sequence_number) == (
            track,
            1,
            20,
            2,
        )
        assert len(media.pcm16) == 640


def test_mark_error_and_stop_frames_are_safe_and_structured() -> None:
    mark = decode_probe_message(
        '{"event":"mark","sequence_number":"3","mark":{"name":"done"}}', 1
    )
    error = decode_probe_message(
        '{"event":"error","payload":{"code":100004,'
        '"title":"invalid_media","detail":"not retained"}}',
        2,
    )
    stop = decode_probe_message('{"event":"stop","sequence_number":"4"}', 3)
    assert isinstance(mark, MarkFrame) and mark.name == "done"
    assert isinstance(error, ErrorFrame) and asdict(error) == {
        "sequence_number": None,
        "code": 100004,
        "title": "invalid_media",
        "host_receive_monotonic_ns": 2,
    }
    assert isinstance(stop, StopFrame)


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        ("not json", "malformed_json"),
        ("[]", "json_not_object"),
        ('{"event":"unknown"}', "unknown_event"),
        (media_raw(track=""), "track_missing"),
        (
            '{"event":"media","sequence_number":"2","stream_id":"s","media":{"track":"inbound","chunk":"1","timestamp":"2","payload":"***"}}',
            "bad_media_payload",
        ),
    ],
)
def test_decoder_rejects_malformed_input(raw: str, category: str) -> None:
    with pytest.raises(ProbeProtocolError, match=category):
        decode_probe_message(raw, 1)


def test_oversized_message_and_format_mismatch_are_rejected() -> None:
    with pytest.raises(ProbeProtocolError, match="oversized_message"):
        decode_probe_message(" " * (64 * 1024 + 1), 1)
    with pytest.raises(ProbeProtocolError, match="media_format_mismatch"):
        validate_media_format(
            MediaFormat("PCMU", 8_000, 1),
            encoding="L16",
            sample_rate=16_000,
            channels=1,
        )


def test_oversized_decoded_payload_and_call_id_mismatch_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_capture, "MAX_WS_MESSAGE_BYTES", 256 * 1024)
    raw = json.dumps(
        {
            "event": "media",
            "sequence_number": "2",
            "stream_id": "memory-only-stream",
            "media": {
                "track": "inbound",
                "chunk": "1",
                "timestamp": "20",
                "payload": base64.b64encode(b"\x00\x00" * 40_000).decode(),
            },
        }
    )
    with pytest.raises(ProbeProtocolError, match="bad_media_payload"):
        decode_probe_message(raw, 1)
    validate_authorized_call_id("expected", "expected")
    with pytest.raises(ProbeProtocolError, match="stream_call_id_mismatch"):
        validate_authorized_call_id("wrong", "expected")


def test_ordering_state_is_separate_by_track_and_reports_anomalies() -> None:
    capture = BoundedCapture(max_bytes_per_track=10_000, max_event_rows=20)
    frames = [
        decode_probe_message(
            media_raw(track="inbound", sequence=2, chunk=1, timestamp=20), 1
        ),
        decode_probe_message(
            media_raw(track="outbound", sequence=3, chunk=1, timestamp=20), 2
        ),
        decode_probe_message(
            media_raw(track="inbound", sequence=5, chunk=3, timestamp=60), 3
        ),
        decode_probe_message(
            media_raw(track="inbound", sequence=4, chunk=2, timestamp=40), 4
        ),
        decode_probe_message(
            media_raw(track="outbound", sequence=3, chunk=1, timestamp=20), 5
        ),
    ]
    for frame in frames:
        assert isinstance(frame, MediaFrame)
        capture.append(frame)
    counts = capture.ordering.counts
    assert counts.sequence_gaps == 1
    assert counts.chunk_gaps == 1
    assert counts.chunk_regressions == 1
    assert counts.sequence_regressions == 1
    assert counts.timestamp_regressions == 1
    assert counts.duplicates == 1
    assert capture.ordering.unresolved


def test_capture_limits_and_per_track_separation() -> None:
    capture = BoundedCapture(max_bytes_per_track=640, max_event_rows=2)
    inbound = decode_probe_message(media_raw(track="inbound"), 1)
    outbound = decode_probe_message(media_raw(track="outbound"), 2)
    assert isinstance(inbound, MediaFrame) and isinstance(outbound, MediaFrame)
    capture.append(inbound)
    capture.append(outbound)
    assert bytes(capture.tracks["inbound"]) == inbound.pcm16
    assert bytes(capture.tracks["outbound"]) == outbound.pcm16
    with pytest.raises(CaptureLimitError, match="capture_limit_reached"):
        capture.append(inbound)


def test_artifact_paths_are_internal_and_private(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactDirectory(tmp_path, "p2-0123456789abcdef")
    manifest = artifacts.write_json("manifest.json", {"run_id": "p2-0123456789abcdef"})
    assert manifest.parent == artifacts.path
    assert os.stat(artifacts.path).st_mode & 0o777 == 0o700
    assert os.stat(manifest).st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="unsafe"):
        artifacts.write_json("../escape.json", {})


def test_global_sequence_accepts_media_mark_dtmf_and_media_interleaving() -> None:
    capture = BoundedCapture(max_bytes_per_track=10_000, max_event_rows=20)
    first = decode_probe_message(media_raw(sequence=1, chunk=1, timestamp=20), 1)
    mark = decode_probe_message(
        '{"event":"mark","sequence_number":"2","mark":{"name":"queued"}}',
        2,
    )
    dtmf = decode_probe_message(
        '{"event":"dtmf","sequence_number":"3","dtmf":{"digit":"5"}}', 3
    )
    second = decode_probe_message(media_raw(sequence=4, chunk=2, timestamp=40), 4)
    assert isinstance(first, MediaFrame) and isinstance(second, MediaFrame)
    capture.append(first)
    capture.observe_non_media(mark)
    capture.observe_non_media(dtmf)
    capture.append(second)
    assert not capture.ordering.unresolved
