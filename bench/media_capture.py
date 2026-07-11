"""Authentication, strict media decoding, integrity tracking, and artifacts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_WS_MESSAGE_BYTES = 64 * 1024
MAX_DECODED_FRAME_BYTES = 64 * 1024
MAX_PENDING_TOKENS = 8
TOKEN_TTL_NS = 60_000_000_000
ALLOWED_TRACKS = frozenset({"inbound", "outbound"})
_SAFE_RUN_ID = re.compile(r"^p2-[0-9a-f]{16}$")


class ProbeProtocolError(ValueError):
    """A named, safe-to-report diagnostic stream failure."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class CaptureLimitError(ProbeProtocolError):
    pass


@dataclass(frozen=True, slots=True)
class StreamAuthorization:
    token: str
    run_id: str
    call_control_id: str
    role: Literal["probe", "agent"]
    expires_ns: int


class StreamTokenStore:
    """Small in-memory one-use token store with constant-time comparisons."""

    def __init__(self, *, maximum: int = MAX_PENDING_TOKENS) -> None:
        if maximum <= 0 or maximum > MAX_PENDING_TOKENS:
            raise ValueError("invalid pending-token limit")
        self._maximum = maximum
        self._pending: list[StreamAuthorization] = []

    def issue(
        self,
        run_id: str,
        call_control_id: str,
        role: Literal["probe", "agent"],
        now_ns: int,
    ) -> str:
        self.remove_expired(now_ns)
        if len(self._pending) >= self._maximum:
            raise ProbeProtocolError("stream_auth_capacity")
        token = secrets.token_urlsafe(32)
        self._pending.append(
            StreamAuthorization(
                token, run_id, call_control_id, role, now_ns + TOKEN_TTL_NS
            )
        )
        return token

    def consume(
        self,
        presented: str,
        *,
        run_id: str,
        role: Literal["probe", "agent"],
        now_ns: int,
    ) -> StreamAuthorization:
        self.remove_expired(now_ns)
        match_index: int | None = None
        for index, authorization in enumerate(self._pending):
            token_matches = hmac.compare_digest(authorization.token, presented)
            run_matches = hmac.compare_digest(authorization.run_id, run_id)
            role_matches = hmac.compare_digest(authorization.role, role)
            if token_matches and run_matches and role_matches:
                match_index = index
        if match_index is None:
            raise ProbeProtocolError("stream_auth_failed")
        return self._pending.pop(match_index)

    def remove_expired(self, now_ns: int) -> None:
        self._pending = [item for item in self._pending if item.expires_ns > now_ns]

    @property
    def pending(self) -> int:
        return len(self._pending)


@dataclass(frozen=True, slots=True)
class ConnectedFrame:
    version: str
    auth_token: str
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class MediaFormat:
    encoding: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class StartFrame:
    sequence_number: int
    stream_id: str
    call_control_id: str
    media_format: MediaFormat
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class MediaFrame:
    sequence_number: int
    stream_id: str
    track: str
    chunk: int
    timestamp: int
    pcm16: bytes
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class MarkFrame:
    sequence_number: int | None
    name: str
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    sequence_number: int | None
    code: int | None
    title: str
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class StopFrame:
    sequence_number: int | None
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class DtmfFrame:
    sequence_number: int | None
    digit: str
    host_receive_monotonic_ns: int


ProbeFrame = (
    ConnectedFrame
    | StartFrame
    | MediaFrame
    | MarkFrame
    | ErrorFrame
    | StopFrame
    | DtmfFrame
)


def _object(value: object, category: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProbeProtocolError(category)
    return value


@overload
def _integer(
    value: object, category: str, *, optional: Literal[False] = False
) -> int: ...


@overload
def _integer(
    value: object, category: str, *, optional: Literal[True]
) -> int | None: ...


def _integer(value: object, category: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ProbeProtocolError(category)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ProbeProtocolError(category)


def decode_probe_message(raw: str, host_receive_monotonic_ns: int) -> ProbeFrame:
    """Strictly parse one Telnyx diagnostic-stream message."""
    if len(raw.encode("utf-8")) > MAX_WS_MESSAGE_BYTES:
        raise ProbeProtocolError("oversized_message")
    try:
        root = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProbeProtocolError("malformed_json") from exc
    data = _object(root, "json_not_object")
    event = data.get("event")
    if not isinstance(event, str):
        raise ProbeProtocolError("missing_event")

    if event == "connected":
        token = data.get("x-telnyx-streaming-auth-token", "")
        return ConnectedFrame(
            version=str(data.get("version", "")),
            auth_token=token if isinstance(token, str) else "",
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    if event == "start":
        start = _object(data.get("start"), "malformed_start")
        media_format = _object(start.get("media_format"), "missing_media_format")
        return StartFrame(
            sequence_number=int(_integer(data.get("sequence_number"), "bad_sequence")),
            stream_id=str(data.get("stream_id", "")),
            call_control_id=str(start.get("call_control_id", "")),
            media_format=MediaFormat(
                encoding=str(media_format.get("encoding", "")),
                sample_rate=int(
                    _integer(media_format.get("sample_rate"), "bad_sample_rate")
                ),
                channels=int(_integer(media_format.get("channels"), "bad_channels")),
            ),
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    if event == "media":
        media = _object(data.get("media"), "malformed_media")
        track = media.get("track")
        if not isinstance(track, str) or track not in ALLOWED_TRACKS:
            raise ProbeProtocolError("track_missing")
        payload = media.get("payload")
        if not isinstance(payload, str):
            raise ProbeProtocolError("bad_media_payload")
        try:
            pcm16 = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProbeProtocolError("bad_media_payload") from exc
        if not pcm16 or len(pcm16) > MAX_DECODED_FRAME_BYTES or len(pcm16) % 2:
            raise ProbeProtocolError("bad_media_payload")
        return MediaFrame(
            sequence_number=int(_integer(data.get("sequence_number"), "bad_sequence")),
            stream_id=str(data.get("stream_id", "")),
            track=track,
            chunk=int(_integer(media.get("chunk"), "bad_chunk")),
            timestamp=int(_integer(media.get("timestamp"), "bad_timestamp")),
            pcm16=pcm16,
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    if event == "mark":
        mark = _object(data.get("mark"), "malformed_mark")
        return MarkFrame(
            sequence_number=_integer(
                data.get("sequence_number"), "bad_sequence", optional=True
            ),
            name=str(mark.get("name", ""))[:128],
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    if event == "error":
        payload = _object(data.get("payload"), "malformed_error")
        return ErrorFrame(
            sequence_number=_integer(
                data.get("sequence_number"), "bad_sequence", optional=True
            ),
            code=_integer(payload.get("code"), "bad_error_code", optional=True),
            title=str(payload.get("title", "unknown"))[:128],
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    if event == "stop":
        return StopFrame(
            sequence_number=_integer(
                data.get("sequence_number"), "bad_sequence", optional=True
            ),
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    if event == "dtmf":
        dtmf = _object(data.get("dtmf"), "malformed_dtmf")
        return DtmfFrame(
            sequence_number=_integer(
                data.get("sequence_number"), "bad_sequence", optional=True
            ),
            digit=str(dtmf.get("digit", ""))[:1],
            host_receive_monotonic_ns=host_receive_monotonic_ns,
        )
    raise ProbeProtocolError("unknown_event")


def validate_media_format(
    actual: MediaFormat, *, encoding: str, sample_rate: int, channels: int
) -> None:
    if (
        actual.encoding != encoding
        or actual.sample_rate != sample_rate
        or actual.channels != channels
    ):
        raise ProbeProtocolError("media_format_mismatch")


def extract_stream_token(
    headers: Mapping[str, str], connected: ConnectedFrame | None
) -> str:
    header = headers.get("x-telnyx-streaming-auth-token", "")
    if header:
        return header
    if connected is not None and connected.auth_token:
        return connected.auth_token
    raise ProbeProtocolError("stream_auth_failed")


@dataclass(slots=True)
class OrderingCounts:
    duplicates: int = 0
    sequence_gaps: int = 0
    sequence_regressions: int = 0
    chunk_gaps: int = 0
    chunk_regressions: int = 0
    timestamp_regressions: int = 0


class OrderingTracker:
    """Global frame sequencing plus track-local media integrity."""

    def __init__(self) -> None:
        self.counts = OrderingCounts()
        self._last: dict[str, tuple[int, int, int]] = {}
        self._last_sequence: int | None = None
        self._seen: set[tuple[str, int, int, int]] = set()

    def observe_frame(self, frame: ProbeFrame) -> None:
        if isinstance(frame, MediaFrame):
            identity = (
                frame.track,
                frame.sequence_number,
                frame.chunk,
                frame.timestamp,
            )
            if identity in self._seen:
                self.counts.duplicates += 1
                return
            self._seen.add(identity)
        sequence = getattr(frame, "sequence_number", None)
        if sequence is not None:
            if self._last_sequence is not None:
                if sequence == self._last_sequence:
                    self.counts.duplicates += 1
                elif sequence < self._last_sequence:
                    self.counts.sequence_regressions += 1
                elif sequence > self._last_sequence + 1:
                    self.counts.sequence_gaps += sequence - self._last_sequence - 1
            self._last_sequence = sequence
        if not isinstance(frame, MediaFrame):
            return
        previous = self._last.get(frame.track)
        if previous is not None:
            _, chunk, timestamp = previous
            if frame.chunk < chunk:
                self.counts.chunk_regressions += 1
            elif frame.chunk > chunk + 1:
                self.counts.chunk_gaps += frame.chunk - chunk - 1
            if frame.timestamp < timestamp:
                self.counts.timestamp_regressions += 1
        self._last[frame.track] = (
            frame.sequence_number,
            frame.chunk,
            frame.timestamp,
        )

    @property
    def unresolved(self) -> bool:
        counts = self.counts
        return any(asdict(counts).values())


def validate_authorized_call_id(actual: str, expected: str) -> None:
    if not actual or not hmac.compare_digest(actual, expected):
        raise ProbeProtocolError("stream_call_id_mismatch")


class BoundedCapture:
    """In-memory per-track PCM with hard byte and row ceilings."""

    def __init__(self, *, max_bytes_per_track: int, max_event_rows: int) -> None:
        if max_bytes_per_track <= 0 or max_event_rows <= 0:
            raise ValueError("capture limits must be positive")
        self._maximum = max_bytes_per_track
        self._max_rows = max_event_rows
        self.tracks: dict[str, bytearray] = {
            track: bytearray() for track in ALLOWED_TRACKS
        }
        self.frames: list[MediaFrame] = []
        self.ordering = OrderingTracker()

    def append(self, frame: MediaFrame) -> None:
        if len(self.frames) >= self._max_rows:
            raise CaptureLimitError("capture_limit_reached")
        target = self.tracks[frame.track]
        if len(target) + len(frame.pcm16) > self._maximum:
            raise CaptureLimitError("capture_limit_reached")
        target.extend(frame.pcm16)
        self.frames.append(frame)
        self.ordering.observe_frame(frame)

    def observe_non_media(self, frame: ProbeFrame) -> None:
        """Account for lifecycle sequence numbers without altering PCM."""
        if isinstance(frame, MediaFrame):
            raise ValueError("media frames must be appended")
        self.ordering.observe_frame(frame)


def new_run_id() -> str:
    return f"p2-{secrets.token_hex(8)}"


class ArtifactDirectory:
    """Safe local ignored artifact directory with append-only raw rows."""

    def __init__(self, root: Path, run_id: str) -> None:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("unsafe run ID")
        self.path = root / run_id
        self.path.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(self.path, 0o700)

    def write_json(self, name: str, value: Mapping[str, object]) -> Path:
        path = self._safe_file(name)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        return path

    def append_jsonl(self, name: str, value: Mapping[str, object]) -> Path:
        path = self._safe_file(name)
        line = json.dumps(value, separators=(",", ":"), sort_keys=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return path

    def _safe_file(self, name: str) -> Path:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("unsafe artifact name")
        return self.path / name


def fixture_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
