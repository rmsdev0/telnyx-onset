"""Offline-first Phase 2 call controller and acoustic probe application.

This module is a separate FastAPI application. Importing it does not alter the
production server, and a call is possible only when both the command-line and
environment live gates are present.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import csv
import hmac
import io
import json
import math
import os
import stat
import statistics
import subprocess
import sys
import time
import wave
from array import array
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import httpx
import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from bench.acoustic_stop import DetectorAnalysis, DetectorConfig, analyze_acoustic_stop
from bench.media_capture import (
    ArtifactDirectory,
    BoundedCapture,
    ConnectedFrame,
    ErrorFrame,
    MarkFrame,
    MediaFormat,
    MediaFrame,
    ProbeProtocolError,
    StartFrame,
    StopFrame,
    StreamAuthorization,
    StreamTokenStore,
    decode_probe_message,
    extract_stream_token,
    fixture_sha256,
    new_run_id,
    validate_authorized_call_id,
    validate_media_format,
)
from onset.agent import VoiceAgent
from onset.media import Connected, Dtmf, Mark, Media, MediaStream, Start, Stop, decode
from onset.prompts import RESTAURANT_CONFIG
from onset.settings import Settings
from onset.telnyx import Call, verify_webhook

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

MAX_WEBHOOK_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = 1024 * 1024
MAX_FIXTURE_SECONDS = 10.0
MAX_CAPTURE_BYTES_PER_TRACK = 4 * 1024 * 1024
MAX_EVENT_ROWS = 20_000
MAX_CALL_SECONDS = 60
MAX_CAPTURE_SECONDS = 60
MAX_LIVE_ATTEMPTS = 3
TEARDOWN_TIMEOUT_SECONDS = 10
STATE_TIMEOUT_SECONDS = 15
FRAME_MS = 20
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
PACING_TOLERANCE_NS = 10_000_000
AUTH_HANDSHAKE_TIMEOUT_SECONDS = 2.0
SEPARATING_SILENCE_MS = 100
FIXTURE_ALIGNMENT_SEARCH_MS = 2_000
MIN_FIXTURE_CORRELATION = 0.85
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = REPOSITORY_ROOT / "bench" / "artifacts"
# Predeclared Phase 2 diagnostic criteria. They are candidates for live
# calibration, not a frozen Phase 3 measurement profile.
MAX_STIMULUS_ENERGY_RATIO_ON_AGENT_TRACK = 0.10
MAX_ABSOLUTE_STIMULUS_CORRELATION = 0.80


class ProbeState(StrEnum):
    DIAL_REQUESTED = "DIAL_REQUESTED"
    LEG_A_IDENTIFIED = "LEG_A_IDENTIFIED"
    LEG_B_IDENTIFIED = "LEG_B_IDENTIFIED"
    LEGS_ANSWERED = "LEGS_ANSWERED"
    BRIDGED = "BRIDGED"
    STREAM_AUTHORIZED = "STREAM_AUTHORIZED"
    STREAM_CONNECTED = "STREAM_CONNECTED"
    MEDIA_FORMAT_VALIDATED = "MEDIA_FORMAT_VALIDATED"
    AGENT_AUDIO_OBSERVED = "AGENT_AUDIO_OBSERVED"
    AGENT_NATURAL_STOP_OBSERVED = "AGENT_NATURAL_STOP_OBSERVED"
    STIMULUS_STARTED = "STIMULUS_STARTED"
    STIMULUS_COMPLETED = "STIMULUS_COMPLETED"
    POST_STIMULUS_AGENT_AUDIO_OBSERVED = "POST_STIMULUS_AGENT_AUDIO_OBSERVED"
    CAPTURE_COMPLETED = "CAPTURE_COMPLETED"


FAILURE_CATEGORIES = frozenset(
    {
        "dial_failed",
        "leg_a_missing",
        "leg_b_missing",
        "answer_timeout",
        "bridge_failed",
        "stream_start_failed",
        "stream_auth_failed",
        "stream_call_id_mismatch",
        "media_format_mismatch",
        "track_missing",
        "track_ambiguous",
        "media_ordering_anomaly",
        "agent_audio_not_observed",
        "natural_stop_not_observed",
        "stimulus_send_failed",
        "fixture_match_missing",
        "fixture_match_ambiguous",
        "stimulus_overlap",
        "stimulus_boundary_ambiguous",
        "post_stimulus_response_not_observed",
        "capture_limit_reached",
        "socket_error",
        "call_hangup",
        "teardown_timeout",
    }
)


class GateFailureError(RuntimeError):
    def __init__(self, category: str) -> None:
        if category not in FAILURE_CATEGORIES:
            raise ValueError(f"unknown gate failure: {category}")
        super().__init__(category)
        self.category = category


class MonotonicClock(Protocol):
    def monotonic_ns(self) -> int: ...


class SystemClock:
    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(frozen=True, slots=True)
class Fixture:
    pcm16: bytes
    sha256: str
    sample_rate: int
    channels: int
    sample_width: int
    frame_bytes: int
    first_active_sample: int
    onset_method: str
    onset_threshold: int

    @property
    def frames(self) -> tuple[bytes, ...]:
        result: list[bytes] = []
        for offset in range(0, len(self.pcm16), self.frame_bytes):
            frame = self.pcm16[offset : offset + self.frame_bytes]
            result.append(frame.ljust(self.frame_bytes, b"\x00"))
        return tuple(result)


def load_fixture(
    path: Path,
    *,
    sample_rate: int = SAMPLE_RATE,
    frame_ms: int = FRAME_MS,
    onset_threshold: int = 256,
    maximum_bytes: int = MAX_FIXTURE_BYTES,
    maximum_seconds: float = MAX_FIXTURE_SECONDS,
) -> Fixture:
    """Validate a bounded local synthetic-speech PCM16 WAV."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("fixture_not_regular") from exc
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ValueError("fixture_not_regular")
    if before.st_size > maximum_bytes:
        raise ValueError("fixture_too_large")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            raw = handle.read(maximum_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ValueError("fixture_read_failed") from exc
    if len(raw) > maximum_bytes:
        raise ValueError("fixture_too_large")
    if (
        opened.st_dev != after.st_dev
        or opened.st_ino != after.st_ino
        or opened.st_size != after.st_size
        or after.st_size != len(raw)
        or before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
    ):
        raise ValueError("fixture_changed_during_read")
    try:
        with wave.open(io.BytesIO(raw), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ValueError("fixture_not_pcm")
            if source.getnchannels() != CHANNELS:
                raise ValueError("fixture_wrong_channels")
            if source.getsampwidth() != SAMPLE_WIDTH:
                raise ValueError("fixture_wrong_sample_width")
            if source.getframerate() != sample_rate:
                raise ValueError("fixture_wrong_sample_rate")
            frame_count = source.getnframes()
            if frame_count / sample_rate > maximum_seconds:
                raise ValueError("fixture_too_long")
            pcm16 = source.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError("fixture_invalid_wav") from exc
    if len(pcm16) % 2:
        raise ValueError("fixture_malformed_pcm")

    first_active: int | None = None
    for index in range(0, len(pcm16), 2):
        value = int.from_bytes(pcm16[index : index + 2], "little", signed=True)
        if abs(value) >= onset_threshold:
            first_active = index // 2
            break
    if first_active is None:
        raise ValueError("fixture_all_silence")
    return Fixture(
        pcm16=pcm16,
        sha256=fixture_sha256(raw),
        sample_rate=sample_rate,
        channels=CHANNELS,
        sample_width=SAMPLE_WIDTH,
        frame_bytes=sample_rate * frame_ms // 1_000 * SAMPLE_WIDTH,
        first_active_sample=first_active,
        onset_method="first_pcm16_sample_abs_gte_threshold",
        onset_threshold=onset_threshold,
    )


@dataclass(frozen=True, slots=True)
class SentFrame:
    frame_index: int
    intended_deadline_ns: int
    send_start_monotonic_ns: int
    send_complete_monotonic_ns: int
    lateness_ns: int
    payload_bytes: int


@dataclass(frozen=True, slots=True)
class PacingSummary:
    frames: tuple[SentFrame, ...]
    maximum_lateness_ns: int
    median_lateness_ns: int
    maximum_send_duration_ns: int
    late_frames: int
    within_tolerance: bool


class TextSender(Protocol):
    async def send_text(self, data: str) -> None: ...


async def send_fixture_paced(
    sender: TextSender,
    fixture: Fixture,
    *,
    clock: MonotonicClock,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    frame_ms: int = FRAME_MS,
    tolerance_ns: int = PACING_TOLERANCE_NS,
) -> PacingSummary:
    """Send exact frames against absolute monotonic deadlines."""
    period_ns = frame_ms * 1_000_000
    origin = clock.monotonic_ns()
    records: list[SentFrame] = []
    for index, frame in enumerate(fixture.frames):
        deadline = origin + index * period_ns
        remaining_ns = deadline - clock.monotonic_ns()
        if remaining_ns > 0:
            await sleep(remaining_ns / 1_000_000_000)
        send_start = clock.monotonic_ns()
        message = json.dumps(
            {
                "event": "media",
                "media": {"payload": base64.b64encode(frame).decode("ascii")},
            },
            separators=(",", ":"),
        )
        await sender.send_text(message)
        send_complete = clock.monotonic_ns()
        records.append(
            SentFrame(
                frame_index=index,
                intended_deadline_ns=deadline,
                send_start_monotonic_ns=send_start,
                send_complete_monotonic_ns=send_complete,
                lateness_ns=max(0, send_start - deadline),
                payload_bytes=len(frame),
            )
        )
    lateness = [record.lateness_ns for record in records]
    durations = [
        record.send_complete_monotonic_ns - record.send_start_monotonic_ns
        for record in records
    ]
    return PacingSummary(
        frames=tuple(records),
        maximum_lateness_ns=max(lateness, default=0),
        median_lateness_ns=int(statistics.median(lateness)) if lateness else 0,
        maximum_send_duration_ns=max(durations, default=0),
        late_frames=sum(value > tolerance_ns for value in lateness),
        within_tolerance=all(value <= tolerance_ns for value in lateness),
    )


@dataclass(frozen=True, slots=True)
class BenchConfig:
    settings: Settings
    agent_number: str
    harness_number: str
    public_wss_base: str
    target_legs: str
    fixture: Fixture
    artifacts_root: Path = ARTIFACT_ROOT
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    call_seconds: int = MAX_CALL_SECONDS
    capture_seconds: int = MAX_CAPTURE_SECONDS
    attempts: int = 1

    def __post_init__(self) -> None:
        if self.target_legs not in {"self", "opposite"}:
            raise ValueError("target_legs must be explicit self or opposite")
        if not self.public_wss_base.startswith("wss://"):
            raise ValueError("public_wss_base must use wss")
        if not self.agent_number or not self.harness_number:
            raise ValueError("both configured phone numbers are required")
        if not self.settings.half_duplex:
            raise ValueError("Phase 2 requires the safe half-duplex listening policy")
        if not 1 <= self.attempts <= MAX_LIVE_ATTEMPTS:
            raise ValueError("live attempt cap exceeded")
        if not 1 <= self.call_seconds <= MAX_CALL_SECONDS:
            raise ValueError("call duration cap exceeded")
        if not 1 <= self.capture_seconds <= MAX_CAPTURE_SECONDS:
            raise ValueError("capture duration cap exceeded")


class SafeCallControl:
    """Bench-only Call Control client that never logs request or response IDs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.telnyx_api_base,
            headers={"Authorization": f"Bearer {settings.telnyx_api_key}"},
            timeout=httpx.Timeout(10.0),
        )

    async def dial(self, *, to: str, from_: str) -> str:
        response = await self._http.post(
            "/calls",
            json={
                "connection_id": self.settings.telnyx_connection_id,
                "to": to,
                "from": from_,
            },
        )
        response.raise_for_status()
        value = response.json().get("data", {}).get("call_control_id")
        if not isinstance(value, str) or not value:
            raise GateFailureError("leg_a_missing")
        return value

    async def action(
        self,
        call_control_id: str,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        response = await self._http.post(
            f"/calls/{call_control_id}/actions/{action}", json=payload or {}
        )
        response.raise_for_status()

    def call(self, call_control_id: str) -> Call:
        return Call(cast("Any", self), call_control_id)

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass(slots=True)
class TrackWindow:
    start_bytes: int
    end_bytes: int | None = None


class ProbeController:
    """One-run state machine. Provider identifiers remain in this object only."""

    def __init__(
        self,
        config: BenchConfig,
        call_control: SafeCallControl,
        *,
        clock: MonotonicClock | None = None,
    ) -> None:
        self.config = config
        self.call_control = call_control
        self.clock = clock or SystemClock()
        self.run_id = new_run_id()
        self.tokens = StreamTokenStore(maximum=4)
        self.leg_a: str | None = None
        self.leg_b: str | None = None
        self.answered: set[str] = set()
        self.streams_started: set[str] = set()
        self.bridge_sent = False
        self.bridge_ready = asyncio.Event()
        self.probe_socket_active = False
        self.states: list[ProbeState] = []
        self.failure: str | None = None
        self.agent: VoiceAgent | None = None
        self._seen_webhooks: set[str] = set()
        self.artifacts = ArtifactDirectory(config.artifacts_root, self.run_id)
        self.started_ns = self.clock.monotonic_ns()
        self.last_transition_ns = self.started_ns
        self.teardown_result = "not_started"
        self._manifest_outcome = "CONDITIONAL GO"
        self._manifest_extra: dict[str, object] = {}
        self._write_manifest("CONDITIONAL GO", attempt_number=1)
        self.artifacts.append_jsonl(
            "events.jsonl",
            {"event": "probe_started", "host_monotonic_ns": self.started_ns},
        )

    def transition(self, state: ProbeState) -> None:
        if state not in self.states:
            self.states.append(state)
            self.last_transition_ns = self.clock.monotonic_ns()
            self.artifacts.append_jsonl(
                "events.jsonl",
                {
                    "event": state.value.lower(),
                    "host_monotonic_ns": self.clock.monotonic_ns(),
                },
            )

    async def watchdog(self) -> None:
        """Enforce named state and hard call-duration timeouts."""
        call_deadline = self.started_ns + self.config.call_seconds * 1_000_000_000
        state_timeout_ns = STATE_TIMEOUT_SECONDS * 1_000_000_000
        while ProbeState.CAPTURE_COMPLETED not in self.states and self.failure is None:
            await asyncio.sleep(0.1)
            now_ns = self.clock.monotonic_ns()
            if now_ns >= call_deadline:
                self.fail("call_hangup")
                break
            if now_ns - self.last_transition_ns < state_timeout_ns:
                continue
            if self.leg_a is None:
                self.fail("leg_a_missing")
            elif self.leg_b is None:
                self.fail("leg_b_missing")
            elif ProbeState.LEGS_ANSWERED not in self.states:
                self.fail("answer_timeout")
            elif ProbeState.BRIDGED not in self.states:
                self.fail("bridge_failed")
            elif ProbeState.STREAM_CONNECTED not in self.states:
                self.fail("stream_start_failed")
            else:
                # Media-window timeouts are classified by the probe socket.
                continue
            break
        if self.failure is not None:
            await self.hangup_both()

    def fail(self, category: str) -> None:
        if category not in FAILURE_CATEGORIES:
            category = "socket_error"
        if self.failure is None:
            self.failure = category
            self.artifacts.append_jsonl(
                "events.jsonl",
                {
                    "event": "gate_failure",
                    "category": category,
                    "host_monotonic_ns": self.clock.monotonic_ns(),
                },
            )
            self._write_manifest(
                "NO-GO",
                failure_category=category,
                terminal_outcome="capture_failed",
                attempt_number=1,
                teardown_result=self.teardown_result,
            )

    async def dial(self) -> None:
        self.transition(ProbeState.DIAL_REQUESTED)
        try:
            self.leg_a = await self.call_control.dial(
                to=self.config.agent_number, from_=self.config.harness_number
            )
            self.transition(ProbeState.LEG_A_IDENTIFIED)
        except Exception as exc:
            self.fail("dial_failed")
            raise GateFailureError("dial_failed") from exc

    def webhook_is_new(self, event_id: str) -> bool:
        if not event_id or event_id in self._seen_webhooks:
            return False
        if len(self._seen_webhooks) >= 1_024:
            self._seen_webhooks.pop()
        self._seen_webhooks.add(event_id)
        return True

    async def dispatch(self, event_type: str, payload: Mapping[str, object]) -> None:
        ccid_value = payload.get("call_control_id")
        ccid = ccid_value if isinstance(ccid_value, str) else ""
        if event_type == "call.initiated":
            direction = payload.get("direction")
            source = payload.get("from")
            destination = payload.get("to")
            if (
                direction == "incoming"
                and isinstance(source, str)
                and isinstance(destination, str)
                and hmac.compare_digest(source, self.config.harness_number)
                and hmac.compare_digest(destination, self.config.agent_number)
            ):
                if self.leg_b is not None and self.leg_b != ccid:
                    return
                self.leg_b = ccid
                self.transition(ProbeState.LEG_B_IDENTIFIED)
                await self.call_control.action(ccid, "answer")
            return

        if event_type == "call.answered" and ccid in {self.leg_a, self.leg_b}:
            self.answered.add(ccid)
            if ccid == self.leg_a and "probe" not in self.streams_started:
                await self._start_stream(ccid, role="probe")
                self.streams_started.add("probe")
            elif ccid == self.leg_b and "agent" not in self.streams_started:
                await self._start_stream(ccid, role="agent")
                self.streams_started.add("agent")
            if self.leg_a and self.leg_b and {self.leg_a, self.leg_b} <= self.answered:
                self.transition(ProbeState.LEGS_ANSWERED)
                if not self.bridge_sent:
                    try:
                        await self.call_control.action(
                            self.leg_a,
                            "bridge",
                            {
                                "call_control_id": self.leg_b,
                                "prevent_double_bridge": True,
                            },
                        )
                    except Exception as exc:
                        self.fail("bridge_failed")
                        raise GateFailureError("bridge_failed") from exc
                    self.bridge_sent = True
            return

        if event_type == "call.bridged" and ccid in {self.leg_a, self.leg_b}:
            if ProbeState.BRIDGED not in self.states:
                self.transition(ProbeState.BRIDGED)
                self.bridge_ready.set()
                self.artifacts.append_jsonl(
                    "events.jsonl",
                    {
                        "event": "legs_bridged",
                        "host_monotonic_ns": self.clock.monotonic_ns(),
                    },
                )
        elif (
            event_type == "call.hangup"
            and ccid in {self.leg_a, self.leg_b}
            and ProbeState.CAPTURE_COMPLETED not in self.states
        ):
            self.fail("call_hangup")

    async def _start_stream(
        self, ccid: str, *, role: Literal["probe", "agent"]
    ) -> None:
        route = role
        token = self.tokens.issue(self.run_id, ccid, route, self.clock.monotonic_ns())
        payload: dict[str, object] = {
            "stream_url": (
                f"{self.config.public_wss_base.rstrip('/')}/ws/{route}/{self.run_id}"
            ),
            "stream_track": "both_tracks" if role == "probe" else "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "L16",
            "stream_bidirectional_sampling_rate": SAMPLE_RATE,
            "stream_bidirectional_target_legs": self.config.target_legs
            if role == "probe"
            else "self",
            "stream_auth_token": token,
        }
        try:
            await self.call_control.action(ccid, "streaming_start", payload)
        except Exception as exc:
            self.fail("stream_start_failed")
            raise GateFailureError("stream_start_failed") from exc
        self.transition(ProbeState.STREAM_AUTHORIZED)

    async def hangup_both(self) -> None:
        async def hangup(ccid: str) -> bool:
            try:
                await self.call_control.action(ccid, "hangup")
            except Exception:
                return False
            return True

        calls = [hangup(ccid) for ccid in (self.leg_a, self.leg_b) if ccid]
        if calls:
            try:
                async with asyncio.timeout(TEARDOWN_TIMEOUT_SECONDS):
                    results = await asyncio.gather(*calls)
                    self.teardown_result = (
                        "both_legs_hung_up" if all(results) else "hangup_failed"
                    )
            except TimeoutError:
                self.teardown_result = "teardown_timeout"
                self.fail("teardown_timeout")
        else:
            self.teardown_result = "no_identified_legs"
        if self.failure is not None:
            self._write_manifest(
                "NO-GO",
                failure_category=self.failure,
                terminal_outcome="capture_failed",
                attempt_number=1,
                teardown_result=self.teardown_result,
            )
        elif ProbeState.CAPTURE_COMPLETED in self.states:
            refreshed = dict(self._manifest_extra)
            refreshed["teardown_result"] = self.teardown_result
            self._write_manifest(self._manifest_outcome, **refreshed)

    def _write_manifest(self, outcome: str, **extra: object) -> None:
        self._manifest_outcome = outcome
        self._manifest_extra = dict(extra)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPOSITORY_ROOT,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
                cwd=REPOSITORY_ROOT,
            ).stdout
        )
        manifest: dict[str, object] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "git_commit": commit,
            "dirty_tree": dirty,
            "python_version": sys.version.split()[0],
            "created_utc": datetime.now(UTC).isoformat(),
            "clock": "time.monotonic_ns",
            "clock_process_id": self.run_id,
            "fixture_sha256": self.config.fixture.sha256,
            "fixture_onset": {
                "method": self.config.fixture.onset_method,
                "threshold_pcm16_abs": self.config.fixture.onset_threshold,
                "first_active_sample": self.config.fixture.first_active_sample,
                "transmitted_frame_index": (
                    self.config.fixture.first_active_sample
                    // (self.config.fixture.frame_bytes // SAMPLE_WIDTH)
                ),
                "sample_offset_within_frame": (
                    self.config.fixture.first_active_sample
                    % (self.config.fixture.frame_bytes // SAMPLE_WIDTH)
                ),
            },
            "expected_media_format": {
                "encoding": "L16",
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
            },
            "target_legs": self.config.target_legs,
            "capture_limits": {
                "call_seconds": self.config.call_seconds,
                "capture_seconds": self.config.capture_seconds,
                "bytes_per_track": MAX_CAPTURE_BYTES_PER_TRACK,
                "event_rows": MAX_EVENT_ROWS,
            },
            "detector_candidate": asdict(self.config.detector),
            "gate_outcome": outcome,
        }
        manifest.update(extra)
        self.artifacts.write_json("manifest.json", manifest)


async def _authenticate_probe_socket(
    ws: WebSocket,
    controller: ProbeController,
    *,
    role: Literal["probe", "agent"],
) -> tuple[StreamAuthorization, ConnectedFrame | None]:
    header_token = ws.headers.get("x-telnyx-streaming-auth-token", "")
    connected: ConnectedFrame | None = None
    if not header_token:
        await ws.accept()
        try:
            async with asyncio.timeout(AUTH_HANDSHAKE_TIMEOUT_SECONDS):
                raw = await ws.receive_text()
        except TimeoutError as exc:
            raise ProbeProtocolError("stream_auth_failed") from exc
        frame = decode_probe_message(raw, controller.clock.monotonic_ns())
        if not isinstance(frame, ConnectedFrame):
            raise ProbeProtocolError("stream_auth_failed")
        connected = frame
    token = extract_stream_token(ws.headers, connected)
    authorization = controller.tokens.consume(
        token,
        run_id=controller.run_id,
        role=role,
        now_ns=controller.clock.monotonic_ns(),
    )
    if header_token:
        await ws.accept()
    return authorization, connected


def validate_artifact_root(root: Path) -> Path:
    """Resolve the live artifact root inside this repository without symlinks."""
    expected = ARTIFACT_ROOT
    if root != expected:
        raise ValueError("artifact_root_mismatch")
    bench_root = (REPOSITORY_ROOT / "bench").resolve()
    resolved_parent = root.parent.resolve(strict=True)
    if resolved_parent != bench_root or root.is_symlink():
        raise ValueError("artifact_root_escape")
    return root


def _candidate_agent_track(
    capture: BoundedCapture, config: DetectorConfig
) -> tuple[str | None, dict[str, DetectorAnalysis], bool]:
    common_bytes = min(len(pcm) for pcm in capture.tracks.values())
    if common_bytes == 0:
        return None, {}, False
    analyses = {
        track: analyze_acoustic_stop(bytes(pcm[:common_bytes]), config)
        for track, pcm in capture.tracks.items()
    }
    candidates = [
        track for track, analysis in analyses.items() if analysis.result is not None
    ]
    if len(candidates) == 1:
        candidate = candidates[0]
        other_tracks = [track for track in analyses if track != candidate]
        mixed = any(
            window.rms_dbfs >= config.activity_threshold_dbfs
            for track in other_tracks
            for window in analyses[track].windows
        )
        if not mixed:
            return candidate, analyses, False
        return None, analyses, True
    return None, analyses, len(candidates) > 1


def _samples(pcm16: bytes) -> array[int]:
    values = array("h")
    values.frombytes(pcm16)
    return values


def _mean_square(pcm16: bytes) -> float:
    values = _samples(pcm16)
    if not values:
        return 0.0
    return sum(int(value) * int(value) for value in values) / len(values)


def _rms_dbfs(pcm16: bytes) -> float:
    energy = _mean_square(pcm16)
    return -math.inf if energy == 0 else 10.0 * math.log10(energy / (32_768**2))


def _absolute_correlation(left: bytes, right: bytes) -> float:
    a = _samples(left)
    b = _samples(right)
    count = min(len(a), len(b))
    if count == 0:
        return 0.0
    sum_a = sum(int(value) for value in a[:count])
    sum_b = sum(int(value) for value in b[:count])
    mean_a = sum_a / count
    mean_b = sum_b / count
    numerator = 0.0
    power_a = 0.0
    power_b = 0.0
    for left_value, right_value in zip(a[:count], b[:count], strict=True):
        centered_a = int(left_value) - mean_a
        centered_b = int(right_value) - mean_b
        numerator += centered_a * centered_b
        power_a += centered_a * centered_a
        power_b += centered_b * centered_b
    denominator = math.sqrt(power_a * power_b)
    return 0.0 if denominator == 0 else abs(numerator / denominator)


def _energy_envelope(pcm16: bytes, frame_samples: int = 320) -> tuple[float, ...]:
    values = _samples(pcm16)
    return tuple(
        math.sqrt(
            sum(int(value) * int(value) for value in values[start:end]) / (end - start)
        )
        for start in range(0, len(values), frame_samples)
        if (end := min(start + frame_samples, len(values))) > start
    )


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_power = sum(value * value for value in left)
    right_power = sum(value * value for value in right)
    denominator = math.sqrt(left_power * right_power)
    return 0.0 if denominator == 0 else numerator / denominator


@dataclass(frozen=True, slots=True)
class FixtureMatch:
    start_byte: int
    end_byte: int
    alignment_frames: int
    energy_envelope_correlation: float
    ambiguous_alignment: bool


def match_fixture_reference(
    candidate: bytes,
    fixture: Fixture,
    *,
    maximum_alignment_ms: int = FIXTURE_ALIGNMENT_SEARCH_MS,
    minimum_correlation: float = MIN_FIXTURE_CORRELATION,
) -> FixtureMatch | None:
    """Match unchanged 16 kHz PCM using a bounded 20 ms energy envelope search."""
    fixture_envelope = _energy_envelope(fixture.pcm16)
    candidate_envelope = _energy_envelope(candidate)
    if not fixture_envelope or len(candidate_envelope) < len(fixture_envelope):
        return None
    max_alignment = min(
        maximum_alignment_ms // FRAME_MS,
        len(candidate_envelope) - len(fixture_envelope),
    )
    scored = [
        (
            _cosine_similarity(
                candidate_envelope[offset : offset + len(fixture_envelope)],
                fixture_envelope,
            ),
            offset,
        )
        for offset in range(max_alignment + 1)
    ]
    score, offset = max(scored)
    if score < minimum_correlation:
        return None
    frame_bytes = fixture.frame_bytes
    return FixtureMatch(
        start_byte=offset * frame_bytes,
        end_byte=(offset + len(fixture_envelope)) * frame_bytes,
        alignment_frames=offset,
        energy_envelope_correlation=score,
        ambiguous_alignment=sum(
            candidate_score >= minimum_correlation
            and math.isclose(candidate_score, score, abs_tol=1e-9)
            for candidate_score, _ in scored
        )
        > 1,
    )


@dataclass(frozen=True, slots=True)
class WindowStatistics:
    active_frame_count: int
    frame_count: int
    rms_dbfs: float
    noise_floor_dbfs: float
    peak_absolute_level: int
    clipping_count: int
    global_sequence_gaps: int
    global_sequence_regressions: int
    global_duplicates: int
    track_chunk_gaps: int
    track_chunk_regressions: int
    track_timestamp_regressions: int


def summarize_window(
    pcm16: bytes,
    detector: DetectorConfig,
    capture: BoundedCapture,
    *,
    track: str,
    start_byte: int,
    end_byte: int,
) -> WindowStatistics:
    envelope = _energy_envelope(pcm16)
    dbfs = tuple(
        -math.inf if value == 0 else 20 * math.log10(value / 32_768)
        for value in envelope
    )
    values = _samples(pcm16)
    counts = capture.ordering.counts
    quiet = tuple(value for value in dbfs if value <= detector.silence_threshold_dbfs)
    position = 0
    selected_frames: list[MediaFrame] = []
    for frame in capture.frames:
        if frame.track != track:
            continue
        frame_end = position + len(frame.pcm16)
        if frame_end > start_byte and position < end_byte:
            selected_frames.append(frame)
        position = frame_end
    chunk_gaps = 0
    chunk_regressions = 0
    timestamp_regressions = 0
    for previous, current in zip(selected_frames, selected_frames[1:], strict=False):
        if current.chunk < previous.chunk:
            chunk_regressions += 1
        elif current.chunk > previous.chunk + 1:
            chunk_gaps += current.chunk - previous.chunk - 1
        if current.timestamp < previous.timestamp:
            timestamp_regressions += 1
    return WindowStatistics(
        active_frame_count=sum(
            value >= detector.activity_threshold_dbfs for value in dbfs
        ),
        frame_count=len(selected_frames),
        rms_dbfs=_rms_dbfs(pcm16),
        noise_floor_dbfs=float(statistics.median(quiet)) if quiet else -math.inf,
        peak_absolute_level=max((abs(int(value)) for value in values), default=0),
        clipping_count=sum(int(value) in {-32_768, 32_767} for value in values),
        global_sequence_gaps=counts.sequence_gaps,
        global_sequence_regressions=counts.sequence_regressions,
        global_duplicates=counts.duplicates,
        track_chunk_gaps=chunk_gaps,
        track_chunk_regressions=chunk_regressions,
        track_timestamp_regressions=timestamp_regressions,
    )


@dataclass(frozen=True, slots=True)
class ContaminationEvidence:
    agent_track_energy: float
    stimulus_track_energy: float
    agent_to_stimulus_energy_ratio: float
    absolute_waveform_correlation: float
    passed: bool


def analyze_contamination(
    agent_window: bytes, stimulus_window: bytes
) -> ContaminationEvidence:
    """Quantify cross-feed during the controlled stimulus-only window."""
    agent_energy = _mean_square(agent_window)
    stimulus_energy = _mean_square(stimulus_window)
    ratio = math.inf if stimulus_energy == 0 else agent_energy / stimulus_energy
    correlation = _absolute_correlation(agent_window, stimulus_window)
    return ContaminationEvidence(
        agent_track_energy=agent_energy,
        stimulus_track_energy=stimulus_energy,
        agent_to_stimulus_energy_ratio=ratio,
        absolute_waveform_correlation=correlation,
        passed=(
            stimulus_energy > 0
            and ratio <= MAX_STIMULUS_ENERGY_RATIO_ON_AGENT_TRACK
            and correlation <= MAX_ABSOLUTE_STIMULUS_CORRELATION
        ),
    )


def _write_energy_csv(
    path: Path,
    capture: BoundedCapture,
    *,
    stimulus_start: Mapping[str, int],
    stimulus_end: Mapping[str, int],
) -> None:
    """Write a derived, sanitized per-frame energy timeline."""
    positions = {track: 0 for track in capture.tracks}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "arrival_index",
                "track",
                "window",
                "host_receive_monotonic_ns",
                "payload_bytes",
                "rms_dbfs",
                "peak_abs",
                "clipped_samples",
            ),
        )
        writer.writeheader()
        for index, frame in enumerate(capture.frames):
            start = positions[frame.track]
            end = start + len(frame.pcm16)
            positions[frame.track] = end
            if end <= stimulus_start[frame.track]:
                window = "pre_stimulus"
            elif start >= stimulus_end[frame.track]:
                window = "post_stimulus"
            else:
                window = "stimulus_only"
            values = _samples(frame.pcm16)
            writer.writerow(
                {
                    "arrival_index": index,
                    "track": frame.track,
                    "window": window,
                    "host_receive_monotonic_ns": frame.host_receive_monotonic_ns,
                    "payload_bytes": len(frame.pcm16),
                    "rms_dbfs": _rms_dbfs(frame.pcm16),
                    "peak_abs": max((abs(int(value)) for value in values), default=0),
                    "clipped_samples": sum(
                        int(value) in {-32_768, 32_767} for value in values
                    ),
                }
            )


def _frame_metadata(frame: MediaFrame) -> dict[str, object]:
    return {
        "event": "media",
        "sequence_number": frame.sequence_number,
        "track": frame.track,
        "chunk": frame.chunk,
        "timestamp": frame.timestamp,
        "payload_bytes": len(frame.pcm16),
        "host_receive_monotonic_ns": frame.host_receive_monotonic_ns,
    }


def _receive_time_for_track_sample(
    capture: BoundedCapture, track: str, sample_index: int
) -> int | None:
    position = 0
    for frame in capture.frames:
        if frame.track != track:
            continue
        samples = len(frame.pcm16) // SAMPLE_WIDTH
        if position <= sample_index < position + samples:
            return frame.host_receive_monotonic_ns
        position += samples
    return None


def _first_active_sample(
    analysis: DetectorAnalysis, threshold_dbfs: float
) -> int | None:
    for window in analysis.windows:
        if window.rms_dbfs >= threshold_dbfs:
            return window.start_sample
    return None


def _write_track_wav(path: Path, pcm16: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as raw, wave.open(raw, "wb") as target:
        target.setnchannels(CHANNELS)
        target.setsampwidth(SAMPLE_WIDTH)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(pcm16)


def create_app(
    config: BenchConfig, call_control: SafeCallControl | None = None
) -> FastAPI:
    """Create the explicit bench-only server; production startup is untouched."""
    client = call_control or SafeCallControl(config.settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        controller = ProbeController(config, client)
        app.state.controller = controller
        dial_task = asyncio.create_task(controller.dial())
        watchdog_task = asyncio.create_task(controller.watchdog())
        try:
            yield
        finally:
            for task in (dial_task, watchdog_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await controller.hangup_both()
            await client.aclose()

    app = FastAPI(title="telnyx-onset-phase2-probe", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "bench-ready"}

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        controller: ProbeController = request.app.state.controller
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Payload too large")
        if not verify_webhook(
            config.settings.telnyx_public_key,
            request.headers,
            bytes(raw),
            config.settings.webhook_tolerance_s,
        ):
            raise HTTPException(status_code=403, detail="Invalid signature")
        try:
            data = json.loads(raw).get("data", {})
            event_id = data.get("id", "")
            event_type = data.get("event_type", "")
            payload = data.get("payload", {})
            if (
                not isinstance(event_id, str)
                or not isinstance(event_type, str)
                or not isinstance(payload, dict)
            ):
                return Response(status_code=200)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return Response(status_code=200)
        if controller.webhook_is_new(event_id):
            try:
                await controller.dispatch(event_type, payload)
            except Exception:
                # Acknowledge once; named failure is already retained locally.
                controller.fail("socket_error")
        return Response(status_code=200)

    @app.websocket("/ws/probe/{run_id}")
    async def probe_ws(ws: WebSocket, run_id: str) -> None:
        controller: ProbeController = ws.app.state.controller
        if not hmac.compare_digest(run_id, controller.run_id):
            await ws.close(code=1008)
            return
        capture: BoundedCapture | None = None
        pacing: PacingSummary | None = None
        agent_track: str | None = None
        stimulus_task: asyncio.Task[PacingSummary] | None = None
        stimulus_start_offsets: dict[str, int] = {}
        stimulus_end_offsets: dict[str, int] = {}
        fixture_match: FixtureMatch | None = None
        stimulus_track: str | None = None
        actual_format: MediaFormat | None = None
        natural_analysis: DetectorAnalysis | None = None
        post_analysis: DetectorAnalysis | None = None
        message_rows = 0
        first_tracks_seen: set[str] = set()
        if controller.probe_socket_active:
            await ws.close(code=1008)
            return
        controller.probe_socket_active = True
        try:
            authorization, connected = await _authenticate_probe_socket(
                ws, controller, role="probe"
            )
            controller.transition(ProbeState.STREAM_CONNECTED)
            if connected is not None:
                controller.artifacts.append_jsonl(
                    "events.jsonl",
                    {
                        "event": "media_connected",
                        "host_monotonic_ns": connected.host_receive_monotonic_ns,
                    },
                )
            capture = BoundedCapture(
                max_bytes_per_track=MAX_CAPTURE_BYTES_PER_TRACK,
                max_event_rows=MAX_EVENT_ROWS,
            )
            async with asyncio.timeout(config.capture_seconds):
                while True:
                    raw = await ws.receive_text()
                    message_rows += 1
                    if message_rows > MAX_EVENT_ROWS:
                        raise ProbeProtocolError("capture_limit_reached")
                    frame = decode_probe_message(raw, controller.clock.monotonic_ns())
                    if isinstance(frame, ConnectedFrame):
                        continue
                    if isinstance(frame, StartFrame):
                        capture.observe_non_media(frame)
                        validate_authorized_call_id(
                            frame.call_control_id, authorization.call_control_id
                        )
                        validate_media_format(
                            frame.media_format,
                            encoding="L16",
                            sample_rate=SAMPLE_RATE,
                            channels=CHANNELS,
                        )
                        actual_format = frame.media_format
                        controller.transition(ProbeState.MEDIA_FORMAT_VALIDATED)
                        controller.artifacts.append_jsonl(
                            "events.jsonl",
                            {
                                "event": "media_format_validated",
                                "host_monotonic_ns": frame.host_receive_monotonic_ns,
                            },
                        )
                        try:
                            async with asyncio.timeout(STATE_TIMEOUT_SECONDS):
                                await controller.bridge_ready.wait()
                        except TimeoutError as exc:
                            raise ProbeProtocolError("bridge_failed") from exc
                        continue
                    if isinstance(frame, MediaFrame):
                        capture.append(frame)
                        controller.artifacts.append_jsonl(
                            "frame_metadata.jsonl", _frame_metadata(frame)
                        )
                        if frame.track not in first_tracks_seen:
                            first_tracks_seen.add(frame.track)
                            controller.artifacts.append_jsonl(
                                "events.jsonl",
                                {
                                    "event": "first_frame_received_by_track",
                                    "track": frame.track,
                                    "host_monotonic_ns": (
                                        frame.host_receive_monotonic_ns
                                    ),
                                },
                            )
                        if agent_track is None:
                            candidate, analyses, ambiguous = _candidate_agent_track(
                                capture, config.detector
                            )
                            if ambiguous:
                                raise ProbeProtocolError("track_ambiguous")
                            if candidate is not None:
                                agent_track = candidate
                                natural_analysis = analyses[candidate]
                                controller.transition(ProbeState.AGENT_AUDIO_OBSERVED)
                                controller.transition(
                                    ProbeState.AGENT_NATURAL_STOP_OBSERVED
                                )
                                natural = natural_analysis.result
                                if natural is None:
                                    raise ProbeProtocolError(
                                        "natural_stop_not_observed"
                                    )
                                active_sample = _first_active_sample(
                                    natural_analysis,
                                    config.detector.activity_threshold_dbfs,
                                )
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": "agent_audio_active",
                                        "track": agent_track,
                                        "sample_index": active_sample,
                                        "host_monotonic_ns": (
                                            _receive_time_for_track_sample(
                                                capture,
                                                agent_track,
                                                active_sample or 0,
                                            )
                                        ),
                                    },
                                )
                                boundary_receive_ns = _receive_time_for_track_sample(
                                    capture,
                                    agent_track,
                                    natural.silence_start_sample,
                                )
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": "candidate_natural_silence_started",
                                        "track": agent_track,
                                        "sample_index": natural.silence_start_sample,
                                        "boundary_frame_receive_monotonic_ns": (
                                            boundary_receive_ns
                                        ),
                                        "resolution_samples": (
                                            natural.resolution_samples
                                        ),
                                    },
                                )
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": "agent_natural_stop_confirmed",
                                        "host_monotonic_ns": (
                                            controller.clock.monotonic_ns()
                                        ),
                                        "backdated_sample_index": (
                                            natural.silence_start_sample
                                        ),
                                    },
                                )
                                stimulus_start_offsets = {
                                    track: len(data)
                                    for track, data in capture.tracks.items()
                                }
                                controller.transition(ProbeState.STIMULUS_STARTED)
                                stimulus_task = asyncio.create_task(
                                    send_fixture_paced(
                                        ws, config.fixture, clock=controller.clock
                                    )
                                )
                        elif stimulus_task is not None and stimulus_task.done():
                            if pacing is None:
                                try:
                                    pacing = stimulus_task.result()
                                except Exception as exc:
                                    raise ProbeProtocolError(
                                        "stimulus_send_failed"
                                    ) from exc
                                if not pacing.within_tolerance:
                                    raise ProbeProtocolError("stimulus_send_failed")
                                active_frame_index = (
                                    config.fixture.first_active_sample
                                    // (config.fixture.frame_bytes // SAMPLE_WIDTH)
                                )
                                active_record = pacing.frames[active_frame_index]
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": "stimulus_first_frame_send_started",
                                        "host_monotonic_ns": (
                                            pacing.frames[0].send_start_monotonic_ns
                                        ),
                                    },
                                )
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": "stimulus_first_frame_send_completed",
                                        "host_monotonic_ns": (
                                            pacing.frames[0].send_complete_monotonic_ns
                                        ),
                                    },
                                )
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": (
                                            "stimulus_first_non_silent_sample_offset"
                                        ),
                                        "transmitted_frame_index": (active_frame_index),
                                        "sample_offset_within_frame": (
                                            config.fixture.first_active_sample
                                            % (
                                                config.fixture.frame_bytes
                                                // SAMPLE_WIDTH
                                            )
                                        ),
                                        "frame_send_start_monotonic_ns": (
                                            active_record.send_start_monotonic_ns
                                        ),
                                        "frame_send_complete_monotonic_ns": (
                                            active_record.send_complete_monotonic_ns
                                        ),
                                    },
                                )
                                controller.artifacts.append_jsonl(
                                    "events.jsonl",
                                    {
                                        "event": "stimulus_last_frame_send_completed",
                                        "host_monotonic_ns": (
                                            pacing.frames[-1].send_complete_monotonic_ns
                                        ),
                                    },
                                )
                                await ws.send_text(
                                    json.dumps(
                                        {
                                            "event": "mark",
                                            "mark": {
                                                "name": "phase2-stimulus-complete"
                                            },
                                        }
                                    )
                                )
                            relative_available = min(
                                len(capture.tracks[track])
                                - stimulus_start_offsets[track]
                                for track in capture.tracks
                            )
                            required_search_bytes = (
                                len(config.fixture.frames) * config.fixture.frame_bytes
                                + FIXTURE_ALIGNMENT_SEARCH_MS
                                * SAMPLE_RATE
                                // 1_000
                                * SAMPLE_WIDTH
                            )
                            if (
                                fixture_match is None
                                and relative_available >= required_search_bytes
                            ):
                                matches = {
                                    track: match_fixture_reference(
                                        bytes(
                                            capture.tracks[track][
                                                stimulus_start_offsets[
                                                    track
                                                ] : stimulus_start_offsets[track]
                                                + relative_available
                                            ]
                                        ),
                                        config.fixture,
                                    )
                                    for track in capture.tracks
                                }
                                matched = [
                                    (track, match)
                                    for track, match in matches.items()
                                    if match is not None
                                ]
                                if not matched:
                                    raise ProbeProtocolError("fixture_match_missing")
                                if len(matched) != 1:
                                    raise ProbeProtocolError("fixture_match_ambiguous")
                                stimulus_track, fixture_match = matched[0]
                                if stimulus_track == agent_track:
                                    raise ProbeProtocolError("track_ambiguous")
                                if fixture_match.ambiguous_alignment:
                                    raise ProbeProtocolError(
                                        "stimulus_boundary_ambiguous"
                                    )
                            if fixture_match is not None and stimulus_track is not None:
                                separation_bytes = (
                                    SEPARATING_SILENCE_MS
                                    * SAMPLE_RATE
                                    // 1_000
                                    * SAMPLE_WIDTH
                                )
                                boundary_relative = (
                                    fixture_match.end_byte + separation_bytes
                                )
                                if relative_available < boundary_relative:
                                    continue
                                stimulus_silence = bytes(
                                    capture.tracks[stimulus_track][
                                        (
                                            stimulus_start_offsets[stimulus_track]
                                            + fixture_match.end_byte
                                        ) : (
                                            stimulus_start_offsets[stimulus_track]
                                            + boundary_relative
                                        )
                                    ]
                                )
                                agent_silence = bytes(
                                    capture.tracks[agent_track][
                                        (
                                            stimulus_start_offsets[agent_track]
                                            + fixture_match.end_byte
                                        ) : (
                                            stimulus_start_offsets[agent_track]
                                            + boundary_relative
                                        )
                                    ]
                                )
                                if (
                                    _rms_dbfs(stimulus_silence)
                                    > config.detector.silence_threshold_dbfs
                                ):
                                    raise ProbeProtocolError(
                                        "stimulus_boundary_ambiguous"
                                    )
                                if (
                                    _rms_dbfs(agent_silence)
                                    > config.detector.silence_threshold_dbfs
                                ):
                                    raise ProbeProtocolError("stimulus_overlap")
                                stimulus_end_offsets = {
                                    track: stimulus_start_offsets[track]
                                    + boundary_relative
                                    for track in capture.tracks
                                }
                                stimulus_agent_audio = bytes(
                                    capture.tracks[agent_track][
                                        stimulus_start_offsets[
                                            agent_track
                                        ] : stimulus_end_offsets[agent_track]
                                    ]
                                )
                                reference_audio = bytes(
                                    capture.tracks[stimulus_track][
                                        stimulus_start_offsets[
                                            stimulus_track
                                        ] : stimulus_end_offsets[stimulus_track]
                                    ]
                                )
                                if not analyze_contamination(
                                    stimulus_agent_audio, reference_audio
                                ).passed:
                                    raise ProbeProtocolError("stimulus_overlap")
                                controller.transition(ProbeState.STIMULUS_COMPLETED)
                                common_post_bytes = min(
                                    len(capture.tracks[track])
                                    - stimulus_end_offsets[track]
                                    for track in capture.tracks
                                )
                                post_audio = bytes(
                                    capture.tracks[agent_track][
                                        stimulus_end_offsets[agent_track] : (
                                            stimulus_end_offsets[agent_track]
                                            + common_post_bytes
                                        )
                                    ]
                                )
                                post = analyze_acoustic_stop(
                                    post_audio, config.detector
                                )
                                if post.result is not None:
                                    stimulus_post = bytes(
                                        capture.tracks[stimulus_track][
                                            stimulus_end_offsets[stimulus_track] : (
                                                stimulus_end_offsets[stimulus_track]
                                                + common_post_bytes
                                            )
                                        ]
                                    )
                                    if any(
                                        value >= config.detector.activity_threshold_dbfs
                                        for value in tuple(
                                            -math.inf
                                            if rms == 0
                                            else 20 * math.log10(rms / 32_768)
                                            for rms in _energy_envelope(stimulus_post)
                                        )
                                    ):
                                        raise ProbeProtocolError("track_ambiguous")
                                    post_analysis = post
                                    controller.transition(
                                        ProbeState.POST_STIMULUS_AGENT_AUDIO_OBSERVED
                                    )
                                    post_active = _first_active_sample(
                                        post,
                                        config.detector.activity_threshold_dbfs,
                                    )
                                    controller.artifacts.append_jsonl(
                                        "events.jsonl",
                                        {
                                            "event": "post_stimulus_agent_audio_active",
                                            "sample_index_after_boundary": post_active,
                                            "host_monotonic_ns": (
                                                frame.host_receive_monotonic_ns
                                            ),
                                        },
                                    )
                                    controller.transition(ProbeState.CAPTURE_COMPLETED)
                                    break
                        continue
                    if isinstance(frame, MarkFrame):
                        capture.observe_non_media(frame)
                        controller.artifacts.append_jsonl(
                            "events.jsonl",
                            {
                                "event": "stimulus_mark_received",
                                "name": frame.name,
                                "host_monotonic_ns": frame.host_receive_monotonic_ns,
                            },
                        )
                    elif isinstance(frame, ErrorFrame):
                        capture.observe_non_media(frame)
                        raise ProbeProtocolError("socket_error")
                    elif isinstance(frame, StopFrame):
                        capture.observe_non_media(frame)
                        break
                    else:
                        capture.observe_non_media(frame)

            if capture.ordering.unresolved:
                raise ProbeProtocolError("media_ordering_anomaly")
            if agent_track is None:
                raise ProbeProtocolError("agent_audio_not_observed")
            if ProbeState.POST_STIMULUS_AGENT_AUDIO_OBSERVED not in controller.states:
                raise ProbeProtocolError("post_stimulus_response_not_observed")
            if not all(capture.tracks.values()):
                raise ProbeProtocolError("track_missing")
            if not stimulus_start_offsets or not stimulus_end_offsets:
                raise ProbeProtocolError("stimulus_send_failed")
            if stimulus_track is None or fixture_match is None:
                raise ProbeProtocolError("fixture_match_missing")
            contamination = analyze_contamination(
                bytes(
                    capture.tracks[agent_track][
                        stimulus_start_offsets[agent_track] : stimulus_end_offsets[
                            agent_track
                        ]
                    ]
                ),
                bytes(
                    capture.tracks[stimulus_track][
                        stimulus_start_offsets[stimulus_track] : stimulus_end_offsets[
                            stimulus_track
                        ]
                    ]
                ),
            )
            if not contamination.passed:
                raise ProbeProtocolError("track_ambiguous")

            for track, data in capture.tracks.items():
                label = (
                    "agent_track.wav" if track == agent_track else "stimulus_track.wav"
                )
                _write_track_wav(controller.artifacts.path / label, bytes(data))
            if pacing is not None:
                for record in pacing.frames:
                    controller.artifacts.append_jsonl(
                        "send_frames.jsonl", asdict(record)
                    )
            _write_energy_csv(
                controller.artifacts.path / "energy_by_frame.csv",
                capture,
                stimulus_start=stimulus_start_offsets,
                stimulus_end=stimulus_end_offsets,
            )
            natural_result = natural_analysis.result if natural_analysis else None
            if natural_result is None:
                raise ProbeProtocolError("natural_stop_not_observed")
            window_statistics = {
                track: {
                    name: asdict(
                        summarize_window(
                            bytes(data[start:end]),
                            config.detector,
                            capture,
                            track=track,
                            start_byte=start,
                            end_byte=end,
                        )
                    )
                    for name, (start, end) in {
                        "A_agent_only_greeting": (
                            0,
                            natural_result.silence_start_sample * 2,
                        ),
                        "B_post_greeting_silence": (
                            natural_result.silence_start_sample * 2,
                            natural_result.confirmation_sample * 2,
                        ),
                        "C_verified_stimulus_reference": (
                            stimulus_start_offsets[track],
                            stimulus_end_offsets[track],
                        ),
                        "D_post_stimulus_agent_response": (
                            stimulus_end_offsets[track],
                            len(data),
                        ),
                    }.items()
                }
                for track, data in capture.tracks.items()
            }
            controller.artifacts.write_json(
                "detector_summary.json",
                {
                    "detector": asdict(config.detector),
                    "natural_stop": asdict(natural_analysis.result)
                    if natural_analysis and natural_analysis.result
                    else None,
                    "post_stimulus_stop": asdict(post_analysis.result)
                    if post_analysis and post_analysis.result
                    else None,
                    "contamination": asdict(contamination),
                    "fixture_match": asdict(fixture_match),
                    "window_statistics": window_statistics,
                    "term": "harness-side acoustic stop candidate",
                },
            )
            controller._write_manifest(
                "CAPTURE_COMPLETE_PENDING_REVIEW",
                actual_media_format=asdict(actual_format) if actual_format else None,
                track_mapping={
                    "agent": agent_track,
                    "stimulus": stimulus_track,
                },
                contamination=asdict(contamination),
                ordering_anomalies=asdict(capture.ordering.counts),
                pacing={
                    "maximum_lateness_ns": pacing.maximum_lateness_ns
                    if pacing
                    else None,
                    "median_lateness_ns": pacing.median_lateness_ns if pacing else None,
                    "late_frames": pacing.late_frames if pacing else None,
                    "within_tolerance": pacing.within_tolerance if pacing else None,
                },
                attempt_number=1,
                terminal_outcome="capture_complete_pending_review",
                teardown_result=controller.teardown_result,
                promotion_prerequisites=(
                    "manual_waveform_agreement",
                    "bounded_calibration_review",
                    "independent_review",
                ),
            )
        except TimeoutError:
            stimulus_failed = False
            if stimulus_task is not None and stimulus_task.done() and pacing is None:
                try:
                    pacing = stimulus_task.result()
                except (asyncio.CancelledError, Exception):
                    stimulus_failed = True
            controller.fail(
                "stimulus_send_failed"
                if stimulus_failed
                else (
                    "natural_stop_not_observed"
                    if agent_track is None
                    else "post_stimulus_response_not_observed"
                )
            )
        except ProbeProtocolError as exc:
            controller.fail(exc.category)
        except (WebSocketDisconnect, asyncio.CancelledError):
            controller.fail("socket_error")
        except Exception:
            controller.fail("socket_error")
        finally:
            if stimulus_task is not None:
                if not stimulus_task.done():
                    stimulus_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await stimulus_task
                elif pacing is None:
                    try:
                        pacing = stimulus_task.result()
                    except (asyncio.CancelledError, Exception):
                        controller.fail("stimulus_send_failed")
            with contextlib.suppress(Exception):
                await ws.close()
            await controller.hangup_both()
            controller.probe_socket_active = False

    @app.websocket("/ws/agent/{run_id}")
    async def agent_ws(ws: WebSocket, run_id: str) -> None:
        controller: ProbeController = ws.app.state.controller
        if not hmac.compare_digest(run_id, controller.run_id):
            await ws.close(code=1008)
            return
        agent: VoiceAgent | None = None
        media: MediaStream | None = None
        try:
            authorization, _ = await _authenticate_probe_socket(
                ws, controller, role="agent"
            )
            while True:
                raw = await ws.receive_text()
                event = decode(raw)
                if isinstance(event, Start):
                    validate_authorized_call_id(
                        event.call_control_id, authorization.call_control_id
                    )
                    if agent is not None:
                        continue
                    media = MediaStream(
                        ws,
                        frame_ms=FRAME_MS,
                        lead_frames=config.settings.inject_lead_frames,
                    )
                    media.start()
                    agent = VoiceAgent(
                        config.settings,
                        controller.call_control.call(event.call_control_id),
                        media,
                        RESTAURANT_CONFIG,
                    )
                    agent.set_call_info(event.call_control_id, event.from_number)
                    media.on_error = agent._on_socket_error
                    controller.agent = agent
                    agent.start()
                elif isinstance(event, Media) and agent is not None:
                    agent.handle_audio(event.pcm16)
                elif isinstance(event, Mark) and agent is not None:
                    name = event.name
                    generation = (
                        int(name.split(":", 1)[1])
                        if name.startswith("speak:") and name.split(":", 1)[1].isdigit()
                        else None
                    )
                    agent.submit_speak_ended(generation)
                elif isinstance(event, Stop):
                    break
                elif isinstance(event, Dtmf | Connected):
                    continue
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            controller.fail("socket_error")
        finally:
            if agent is not None:
                agent.submit_hangup()
                task = agent.run_task
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(task, timeout=TEARDOWN_TIMEOUT_SECONDS)
                else:
                    await agent.aclose()
            if media is not None:
                await media.aclose()
            controller.agent = None
            with contextlib.suppress(Exception):
                await ws.close()

    return app


def _live_config(arguments: argparse.Namespace) -> BenchConfig:
    if os.environ.get("BENCH_LIVE") != "1":
        raise SystemExit("live gate closed: BENCH_LIVE=1 is also required")
    fixture_value = arguments.fixture
    target_legs = arguments.target_legs
    if fixture_value is None or target_legs is None:
        raise SystemExit("--fixture and --target-legs are required for live mode")
    settings = Settings()  # type: ignore[call-arg]
    required = {
        "BENCH_AGENT_NUMBER": os.environ.get("BENCH_AGENT_NUMBER", ""),
        "BENCH_HARNESS_NUMBER": os.environ.get("BENCH_HARNESS_NUMBER", ""),
        "BENCH_PUBLIC_WSS_BASE": os.environ.get("BENCH_PUBLIC_WSS_BASE", ""),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "missing required trusted live configuration: " + ", ".join(missing)
        )
    return BenchConfig(
        settings=settings,
        agent_number=required["BENCH_AGENT_NUMBER"],
        harness_number=required["BENCH_HARNESS_NUMBER"],
        public_wss_base=required["BENCH_PUBLIC_WSS_BASE"],
        target_legs=target_legs,
        fixture=load_fixture(fixture_value),
        artifacts_root=validate_artifact_root(ARTIFACT_ROOT),
    )


def _run_live_preflight() -> None:
    """Refuse dialing unless ignore, tests, lint, and types are currently clean."""
    checks = (
        (["git", "check-ignore", "-q", str(ARTIFACT_ROOT)], "artifact ignore rule"),
        ([sys.executable, "-m", "pytest", "-q"], "offline tests"),
        ([sys.executable, "-m", "ruff", "check", "onset/", "bench/", "tests/"], "Ruff"),
        ([sys.executable, "-m", "mypy", "onset/", "bench/", "tests/"], "mypy"),
    )
    for command, label in checks:
        result = subprocess.run(command, check=False, cwd=REPOSITORY_ROOT)
        if result.returncode != 0:
            raise SystemExit(f"live preflight failed: {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 acoustic-boundary probe")
    parser.add_argument(
        "--live", action="store_true", help="allow one bounded live attempt"
    )
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--target-legs", choices=("self", "opposite"))
    arguments = parser.parse_args()
    if not arguments.live:
        print("offline safety gate: no call placed; run the offline test suite")
        return
    config = _live_config(arguments)
    _run_live_preflight()
    print(
        "live configuration accepted: one attempt, 60-second hard cap, "
        f"target_legs={config.target_legs}, fixture_sha256={config.fixture.sha256}"
    )
    uvicorn.run(
        create_app(config),
        host=config.settings.host,
        port=config.settings.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
