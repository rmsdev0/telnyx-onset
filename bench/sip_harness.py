"""External SIP media-endpoint harness core (Amendment 1, SIP_HARNESS_SPEC).

This module holds everything that can be validated offline: the derived
emission fixture, the G.711 encoder, and the pure fail-closed session state
machine that the SIP media layer drives through a pull/push interface. The
pjsua2 adapter lives separately and is exercised only in the bounded live
attempt.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import wave
from array import array
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bench.acoustic_probe import (
    Fixture,
    decode_pcmu_8k_to_pcm16_16k,
    match_fixture_reference,
)
from bench.acoustic_stop import DetectorConfig, analyze_acoustic_stop

if TYPE_CHECKING:
    from pathlib import Path

    from bench.media_capture import ArtifactDirectory

FRAME_MS = 20
SAMPLE_RATE_8K = 8_000
ANALYSIS_SAMPLE_RATE = 16_000
PCMU_FRAME_BYTES = 160
PCM16_8K_FRAME_BYTES = 320
HARD_CALL_CAP_NS = 60 * 1_000_000_000
STATE_TIMEOUT_NS = 15 * 1_000_000_000
GREETING_HORIZON_NS = 15 * 1_000_000_000
SEPARATING_SILENCE_NS = 100 * 1_000_000
ECHO_SEARCH_MS = 4_000
# Cumulative tx handoff-cadence drift bound: half a frame period.
TX_DRIFT_BOUND_NS = FRAME_MS * 1_000_000 // 2
ANALYSIS_INTERVAL_FRAMES = 10
MAX_RECORDED_SAMPLES_8K = (60 + 10) * SAMPLE_RATE_8K
DECIMATION_RULE = "hamming63_lowpass3400_group_delay_compensated_decimate2"

SIP_FAILURE_CATEGORIES = frozenset(
    {
        "dial_failed",
        "answer_timeout",
        "stream_start_failed",
        "media_format_mismatch",
        "media_ordering_anomaly",
        "agent_audio_not_observed",
        "natural_stop_not_observed",
        "stimulus_send_failed",
        "stimulus_overlap",
        "post_stimulus_response_not_observed",
        "capture_limit_reached",
        "socket_error",
        "call_hangup",
        "teardown_timeout",
        "stimulus_boundary_ambiguous",
        "rx_timeline_discontinuity",
        "post_stimulus_echo_detected",
    }
)


class SipHarnessError(ValueError):
    """A named, safe-to-report harness failure."""

    def __init__(self, category: str) -> None:
        if category not in SIP_FAILURE_CATEGORIES:
            category = "socket_error"
        super().__init__(category)
        self.category = category


def encode_pcm16_to_pcmu(pcm16: bytes) -> bytes:
    """Standard G.711 mu-law encoding of PCM16 samples."""
    if len(pcm16) % 2:
        raise ValueError("malformed odd-byte PCM16 input")
    samples = array("h")
    samples.frombytes(pcm16)
    out = bytearray(len(samples))
    for index, sample in enumerate(samples):
        value = int(sample)
        sign = 0x80 if value < 0 else 0
        if value < 0:
            value = -value
        value = min(value, 32_635)
        value += 0x84
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not value & mask:
            exponent -= 1
            mask >>= 1
        mantissa = (value >> (exponent + 3)) & 0x0F
        out[index] = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return bytes(out)


def _low_pass_decimate_16k_to_8k(pcm16: bytes) -> bytes:
    """Anti-aliased 2:1 decimation with group-delay compensation.

    A symmetric 63-tap Hamming-windowed sinc low-pass (cutoff 3.4 kHz at
    16 kHz) is applied with its (N-1)/2-sample delay compensated so the
    onset position is preserved, then every second sample is kept.
    Drop-every-other decimation is explicitly rejected by the specification.
    """
    taps = 63
    cutoff = 3_400 / 16_000
    center = (taps - 1) // 2
    kernel: list[float] = []
    for n in range(taps):
        m = n - center
        ideal = 2 * cutoff if m == 0 else math.sin(2 * math.pi * cutoff * m) / (
            math.pi * m
        )
        window = 0.54 - 0.46 * math.cos(2 * math.pi * n / (taps - 1))
        kernel.append(ideal * window)
    gain = sum(kernel)
    kernel = [k / gain for k in kernel]

    samples = array("h")
    samples.frombytes(pcm16)
    values = list(samples)
    filtered: list[int] = []
    # Output sample i uses input window centered at i (delay compensated).
    for i in range(0, len(values), 2):
        acc = 0.0
        for n, k in enumerate(kernel):
            j = i + n - center
            if 0 <= j < len(values):
                acc += k * values[j]
        filtered.append(max(-32_768, min(32_767, round(acc))))
    return array("h", filtered).tobytes()


@dataclass(frozen=True, slots=True)
class EmittedFixture:
    """The frozen 8 kHz derivative actually transmitted on the wire."""

    pcm16_8k: bytes
    pcmu_frames: tuple[bytes, ...]
    sha256: str
    first_active_sample: int
    decimation_rule: str


def derive_emission_fixture(
    fixture: Fixture, *, onset_threshold: int = 256
) -> EmittedFixture:
    pcm8k = _low_pass_decimate_16k_to_8k(fixture.pcm16)
    remainder = len(pcm8k) % PCM16_8K_FRAME_BYTES
    if remainder:
        pcm8k += bytes(PCM16_8K_FRAME_BYTES - remainder)
    samples = array("h")
    samples.frombytes(pcm8k)
    first_active = next(
        (i for i, value in enumerate(samples) if abs(int(value)) >= onset_threshold),
        None,
    )
    if first_active is None:
        raise ValueError("derived fixture contains no active sample")
    pcmu = encode_pcm16_to_pcmu(pcm8k)
    frames = tuple(
        pcmu[start : start + PCMU_FRAME_BYTES]
        for start in range(0, len(pcmu), PCMU_FRAME_BYTES)
    )
    return EmittedFixture(
        pcm16_8k=pcm8k,
        pcmu_frames=frames,
        sha256=hashlib.sha256(pcm8k).hexdigest(),
        first_active_sample=first_active,
        decimation_rule=DECIMATION_RULE,
    )


@dataclass(frozen=True, slots=True)
class RxFrame:
    """One received, jitter-buffered PCMU frame with its RTP identity."""

    pcmu: bytes
    rtp_sequence: int
    rtp_timestamp: int
    host_receive_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class SipSessionConfig:
    fixture: Fixture
    emitted: EmittedFixture
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    call_cap_ns: int = HARD_CALL_CAP_NS
    state_timeout_ns: int = STATE_TIMEOUT_NS
    greeting_horizon_ns: int = GREETING_HORIZON_NS
    separating_silence_ns: int = SEPARATING_SILENCE_NS
    echo_search_ms: int = ECHO_SEARCH_MS


_SILENCE_PCMU_FRAME = encode_pcm16_to_pcmu(bytes(PCM16_8K_FRAME_BYTES))


def _sustained_activity_anchor(pcm16: bytes, config: DetectorConfig) -> int | None:
    """First sample of the first run of active windows totalling the arm time.

    Mirrors analyze_acoustic_stop's arming rule so an isolated blip (the
    live-attempt-6 false-anchor failure mode) cannot anchor Window A.
    """
    samples = array("h")
    samples.frombytes(pcm16)
    size = config.window_samples
    minimum = config.sample_rate * config.minimum_active_ms // 1_000
    run_start: int | None = None
    run_samples = 0
    for start in range(0, len(samples), size):
        end = min(start + size, len(samples))
        total = 0
        for value in samples[start:end]:
            total += int(value) * int(value)
        rms = math.sqrt(total / (end - start)) if end > start else 0.0
        dbfs = -math.inf if rms == 0 else 20.0 * math.log10(rms / 32_768.0)
        if dbfs >= config.activity_threshold_dbfs:
            if run_start is None:
                run_start = start
            run_samples += end - start
            if run_samples >= minimum:
                return run_start
        else:
            run_start = None
            run_samples = 0
    return None


class SipSession:
    """Pure fail-closed measurement state machine.

    The media layer reports answered/rx/BYE/renegotiation events and pulls tx
    frames on its media clock; the session owns every methodology decision
    and never performs I/O beyond the injected artifact directory.
    """

    def __init__(
        self,
        config: SipSessionConfig,
        artifacts: ArtifactDirectory,
        *,
        dial_requested_ns: int,
    ) -> None:
        self.config = config
        self.artifacts = artifacts
        self.dial_requested_ns = dial_requested_ns
        self.outcome: str | None = None
        self.teardown_result: str | None = None
        self.answered_ns: int | None = None
        # rx state
        self._rx_pcmu = bytearray()
        self._rx_16k = bytearray()
        self._rx_frames: list[tuple[int, int]] = []  # (sample16k_offset, host_ns)
        self._rx_frame_rms: list[tuple[int, float]] = []  # (host_ns, rms_dbfs)
        self._last_rtp_sequence: int | None = None
        self._last_rtp_timestamp: int | None = None
        self._rx_frame_count = 0
        self.rx_counters = {
            "sequence_gaps": 0,
            "sequence_regressions": 0,
            "timestamp_anomalies": 0,
        }
        # windows
        self.anchor_ns: int | None = None
        self.anchor_sample_16k: int | None = None
        self.greeting_stop_ns: int | None = None
        self._greeting_stop_sample_16k: int | None = None
        # tx state
        self._tx_pcm_8k = bytearray()
        self._tx_index = 0
        self._tx_start_ns: int | None = None
        self._fixture_armed = False
        self._fixture_frame_next = 0
        self.emission_boundary_ns: int | None = None
        self.emission_boundary_tx_sample: int | None = None
        self.fixture_end_ns: int | None = None
        self._separation_confirmed_ns: int | None = None
        self._window_d_open_sample_16k: int | None = None
        self.delivery_counters: dict[str, object] | None = None
        self._event("dial_requested", dial_requested_ns)

    # ------------------------------------------------------------------ io

    def _event(self, name: str, host_ns: int, **extra: object) -> None:
        row: dict[str, object] = {"event": name, "host_monotonic_ns": host_ns}
        row.update(extra)
        self.artifacts.append_jsonl("events.jsonl", row)

    def _fail(self, category: str, host_ns: int) -> None:
        if self.outcome is not None:
            return
        error = SipHarnessError(category)
        self.outcome = error.category
        self._event("gate_failure", host_ns, category=error.category)

    # -------------------------------------------------------------- inputs

    def handle_answered(self, codec: str, now_ns: int) -> None:
        if self.outcome is not None:
            return
        self._event(
            "media_format_observed", now_ns, negotiated_codec=str(codec)[:16]
        )
        if codec != "PCMU":
            self._fail("media_format_mismatch", now_ns)
            return
        self.answered_ns = now_ns
        self._event("media_format_validated", now_ns)

    def handle_renegotiation(self, changed: bool, now_ns: int) -> None:
        if changed:
            self._event("mid_call_renegotiation", now_ns)
            self._fail("media_format_mismatch", now_ns)
        else:
            self._event("session_refresh", now_ns)

    def handle_telephone_event(self, now_ns: int) -> None:
        # RFC 2833 / comfort-noise payloads: logged, excluded from analysis.
        self._event("telephone_event_excluded", now_ns)

    def handle_remote_bye(self, now_ns: int) -> None:
        if self.outcome is None:
            self._fail("call_hangup", now_ns)
        if self.teardown_result is None:
            self.teardown_result = "remote_bye"

    def handle_rx_frame(self, frame: RxFrame) -> None:
        if self.outcome is not None:
            return
        if self.answered_ns is None:
            # Early media is excluded from the measured timeline.
            self._event("early_media_excluded", frame.host_receive_monotonic_ns)
            return
        if len(frame.pcmu) != PCMU_FRAME_BYTES:
            self._event(
                "media_frame_size_mismatch",
                frame.host_receive_monotonic_ns,
                source_payload_bytes=len(frame.pcmu),
            )
            self._fail("media_format_mismatch", frame.host_receive_monotonic_ns)
            return
        self._observe_rtp_identity(frame)
        if self.outcome is not None:
            return
        if len(self._rx_pcmu) + len(frame.pcmu) > MAX_RECORDED_SAMPLES_8K:
            self._fail("capture_limit_reached", frame.host_receive_monotonic_ns)
            return
        pcm16 = decode_pcmu_8k_to_pcm16_16k(frame.pcmu)
        self._rx_frames.append((len(self._rx_16k), frame.host_receive_monotonic_ns))
        self._rx_pcmu.extend(frame.pcmu)
        self._rx_16k.extend(pcm16)
        rms = self._frame_rms_dbfs(pcm16)
        self._rx_frame_rms.append((frame.host_receive_monotonic_ns, rms))
        self.artifacts.append_jsonl(
            "frame_metadata.jsonl",
            {
                "track": "rx",
                "rtp_sequence": frame.rtp_sequence,
                "rtp_timestamp": frame.rtp_timestamp,
                "host_receive_monotonic_ns": frame.host_receive_monotonic_ns,
                "rms_dbfs": None if rms == -math.inf else round(rms, 2),
                "payload_bytes": len(pcm16),
                "source_payload_bytes": len(frame.pcmu),
            },
        )
        self._rx_frame_count += 1
        self._check_stimulus_overlap(frame.host_receive_monotonic_ns, rms)
        if self._rx_frame_count % ANALYSIS_INTERVAL_FRAMES == 0:
            self._analyze(frame.host_receive_monotonic_ns)

    def pull_tx_frame(self, now_ns: int) -> bytes:
        """Media-clock frame request; returns one 160-byte PCMU payload."""
        if self._tx_start_ns is None:
            self._tx_start_ns = now_ns
        ideal_ns = self._tx_start_ns + self._tx_index * FRAME_MS * 1_000_000
        if abs(now_ns - ideal_ns) > TX_DRIFT_BOUND_NS and self.outcome is None:
            self._event(
                "tx_cadence_drift",
                now_ns,
                drift_ns=now_ns - ideal_ns,
                frame_index=self._tx_index,
            )
            self._fail("media_ordering_anomaly", now_ns)
        frame = _SILENCE_PCMU_FRAME
        emitted = self.config.emitted
        if self._fixture_armed and self._fixture_frame_next < len(
            emitted.pcmu_frames
        ):
            index = self._fixture_frame_next
            frame = emitted.pcmu_frames[index]
            onset_frame = emitted.first_active_sample // PCMU_FRAME_BYTES
            if index == onset_frame and self.emission_boundary_ns is None:
                self.emission_boundary_tx_sample = (
                    self._tx_index * PCMU_FRAME_BYTES + emitted.first_active_sample
                )
                self.emission_boundary_ns = now_ns
                self._event(
                    "stimulus_emission_boundary",
                    now_ns,
                    tx_sample_8k=self.emission_boundary_tx_sample,
                )
            self._fixture_frame_next += 1
            if self._fixture_frame_next == len(emitted.pcmu_frames):
                self.fixture_end_ns = now_ns + FRAME_MS * 1_000_000
                self._event("stimulus_transmission_completed", self.fixture_end_ns)
        self._tx_pcm_8k.extend(
            emitted.pcm16_8k[
                (self._fixture_frame_next - 1)
                * PCM16_8K_FRAME_BYTES : self._fixture_frame_next
                * PCM16_8K_FRAME_BYTES
            ]
            if frame is not _SILENCE_PCMU_FRAME
            else bytes(PCM16_8K_FRAME_BYTES)
        )
        self._tx_index += 1
        return frame

    def tick(self, now_ns: int) -> None:
        """Deadline enforcement; the media layer calls this periodically."""
        if self.outcome is not None:
            return
        if now_ns - self.dial_requested_ns >= self.config.call_cap_ns:
            self._fail(self._cap_category(), now_ns)
            return
        if self.answered_ns is None:
            if now_ns - self.dial_requested_ns >= self.config.state_timeout_ns:
                self._fail("answer_timeout", now_ns)
            return
        if not self._rx_frames:
            if now_ns - self.answered_ns >= self.config.state_timeout_ns:
                self._fail("stream_start_failed", now_ns)
            return
        horizon_origin = self.anchor_ns or self.answered_ns
        if (
            self.greeting_stop_ns is None
            and now_ns - horizon_origin >= self.config.greeting_horizon_ns
        ):
            self._fail(
                "natural_stop_not_observed"
                if self.anchor_ns is not None
                else "agent_audio_not_observed",
                now_ns,
            )
            return
        if (
            self.fixture_end_ns is not None
            and self._separation_confirmed_ns is None
            and now_ns
            - self.fixture_end_ns
            > self.config.separating_silence_ns + self.config.state_timeout_ns
        ):
            # Continuous rx activity after the fixture leaves no valid
            # boundary: the run has no defensible Window C/D split.
            self._fail("stimulus_boundary_ambiguous", now_ns)

    def set_delivery_counters(self, counters: dict[str, object]) -> None:
        self.delivery_counters = dict(counters)

    def report_rx_loss(self, packets: int, now_ns: int) -> None:
        """Stream-stat loss reported by the media layer.

        The pull/push port surface has no per-packet RTP headers, so the
        adapter polls stack stream statistics and reports increases here;
        loss once the measured windows have begun fails closed.
        """
        if packets <= 0:
            return
        self.rx_counters["reported_loss"] = (
            int(self.rx_counters.get("reported_loss", 0)) + packets
        )
        self._event("rx_stream_loss_reported", now_ns, packets=packets)
        if self.anchor_ns is not None:
            self._fail("rx_timeline_discontinuity", now_ns)

    # ------------------------------------------------------------ analysis

    def _cap_category(self) -> str:
        if self.emission_boundary_ns is not None:
            return "post_stimulus_response_not_observed"
        if self.anchor_ns is not None:
            return "natural_stop_not_observed"
        return "agent_audio_not_observed"

    @staticmethod
    def _frame_rms_dbfs(pcm16: bytes) -> float:
        samples = array("h")
        samples.frombytes(pcm16)
        if not samples:
            return -math.inf
        total = 0
        for value in samples:
            total += int(value) * int(value)
        rms = math.sqrt(total / len(samples))
        return -math.inf if rms == 0 else 20.0 * math.log10(rms / 32_768.0)

    def _observe_rtp_identity(self, frame: RxFrame) -> None:
        expected_step = PCMU_FRAME_BYTES
        if self._last_rtp_sequence is not None:
            if frame.rtp_sequence <= self._last_rtp_sequence:
                self.rx_counters["sequence_regressions"] += 1
            elif frame.rtp_sequence != self._last_rtp_sequence + 1:
                self.rx_counters["sequence_gaps"] += 1
        if self._last_rtp_timestamp is not None and frame.rtp_timestamp != (
            self._last_rtp_timestamp + expected_step
        ):
            self.rx_counters["timestamp_anomalies"] += 1
        discontinuity = (
            self._last_rtp_sequence is not None
            and (
                frame.rtp_sequence != self._last_rtp_sequence + 1
                or frame.rtp_timestamp
                != (self._last_rtp_timestamp or 0) + expected_step
            )
        )
        self._last_rtp_sequence = frame.rtp_sequence
        self._last_rtp_timestamp = frame.rtp_timestamp
        if discontinuity:
            self._event(
                "rx_rtp_discontinuity", frame.host_receive_monotonic_ns
            )
            # Fail closed once the measured windows have begun; earlier
            # discontinuities (setup phase) are recorded only.
            if self.anchor_ns is not None:
                self._fail(
                    "rx_timeline_discontinuity", frame.host_receive_monotonic_ns
                )

    def _check_stimulus_overlap(self, host_ns: int, rms_dbfs: float) -> None:
        if (
            self.emission_boundary_ns is not None
            and self.fixture_end_ns is not None
            and self.emission_boundary_ns <= host_ns <= self.fixture_end_ns
            and rms_dbfs >= self.config.detector.activity_threshold_dbfs
        ):
            self._fail("stimulus_overlap", host_ns)

    def _analyze(self, now_ns: int) -> None:
        detector = self.config.detector
        rx = bytes(self._rx_16k)
        if self.anchor_ns is None:
            anchor_sample = _sustained_activity_anchor(rx, detector)
            if anchor_sample is not None:
                self.anchor_sample_16k = anchor_sample
                self.anchor_ns = self._host_time_for_sample(anchor_sample)
                self._event(
                    "window_a_anchor",
                    self.anchor_ns,
                    sample_16k=anchor_sample,
                )
        if self.anchor_ns is None:
            return
        if self.greeting_stop_ns is None:
            analysis = analyze_acoustic_stop(rx, detector)
            if analysis.result is not None:
                stop_sample = analysis.result.silence_start_sample
                self._greeting_stop_sample_16k = stop_sample
                self.greeting_stop_ns = self._host_time_for_sample(stop_sample)
                self._event(
                    "agent_natural_stop_confirmed",
                    now_ns,
                    backdated_sample_16k=stop_sample,
                )
                self._fixture_armed = True
                self._event("stimulus_armed", now_ns)
            return
        if self.fixture_end_ns is None:
            return
        if self._separation_confirmed_ns is None:
            self._evaluate_separating_silence(now_ns)
            return
        self._evaluate_window_d(now_ns)

    def _evaluate_separating_silence(self, now_ns: int) -> None:
        """Find the first contiguous 100 ms of rx silence after the fixture.

        Trailing rx activity (delayed fixture audio in transit) moves the
        boundary rather than becoming a response, per the frozen window
        rules; tick() fails the run as stimulus_boundary_ambiguous if no
        qualifying interval ever appears.
        """
        assert self.fixture_end_ns is not None
        candidates = [
            (host, rms)
            for host, rms in self._rx_frame_rms
            if host >= self.fixture_end_ns
        ]
        needed_ms = self.config.separating_silence_ns // 1_000_000
        run: list[int] = []
        for host, rms in candidates:
            if rms <= self.config.detector.silence_threshold_dbfs:
                run.append(host)
                if len(run) * FRAME_MS >= needed_ms:
                    boundary_ns = run[0] + self.config.separating_silence_ns
                    self._separation_confirmed_ns = boundary_ns
                    self._window_d_open_sample_16k = (
                        self._first_sample_at_or_after(boundary_ns)
                    )
                    self._event(
                        "separating_silence_confirmed",
                        now_ns,
                        boundary_host_ns=boundary_ns,
                    )
                    return
            else:
                run = []

    def _evaluate_window_d(self, now_ns: int) -> None:
        assert self._window_d_open_sample_16k is not None
        post = bytes(self._rx_16k[self._window_d_open_sample_16k * 2 :])
        anchor = _sustained_activity_anchor(post, self.config.detector)
        if anchor is None:
            return
        # The echo gate can only rule on activity once enough post-boundary
        # audio exists to align the full fixture at the activity's offset;
        # completing earlier would let a fast-arriving echo through unjudged.
        required = anchor * 2 + len(self.config.fixture.pcm16)
        if len(post) < required:
            return
        echo = match_fixture_reference(
            post,
            self.config.fixture,
            maximum_alignment_ms=self.config.echo_search_ms,
        )
        if echo is not None:
            self._fail("post_stimulus_echo_detected", now_ns)
            return
        self.outcome = "capture_complete_pending_review"
        self._event("capture_completed", now_ns)

    def _host_time_for_sample(self, sample_16k: int) -> int:
        byte_offset = sample_16k * 2
        best = self._rx_frames[0][1] if self._rx_frames else 0
        for offset, host_ns in self._rx_frames:
            if offset <= byte_offset:
                best = host_ns
            else:
                break
        return best

    def _first_sample_at_or_after(self, host_ns: int) -> int:
        for offset, frame_host in self._rx_frames:
            if frame_host >= host_ns:
                return offset // 2
        return len(self._rx_16k) // 2

    # ------------------------------------------------------------ teardown

    def finalize(
        self,
        *,
        teardown_result: str,
        now_ns: int,
        extra: dict[str, object] | None = None,
    ) -> None:
        if self.teardown_result is None:
            self.teardown_result = teardown_result
        self._write_wav("rx_8k.wav", bytes(self._rx_pcmu), source="pcmu")
        self._write_wav("tx_8k.wav", bytes(self._tx_pcm_8k), source="pcm16")
        emitted = self.config.emitted
        manifest: dict[str, object] = {
            "schema_version": 1,
            "harness": "sip_media_endpoint",
            "gate_outcome": (
                "CAPTURE_COMPLETE_PENDING_REVIEW"
                if self.outcome == "capture_complete_pending_review"
                else "NO-GO"
            ),
            "failure_category": (
                None
                if self.outcome == "capture_complete_pending_review"
                else self.outcome
            ),
            "terminal_outcome": (
                "capture_complete_pending_review"
                if self.outcome == "capture_complete_pending_review"
                else "capture_failed"
            ),
            "attempt_number": 1,
            "teardown_result": self.teardown_result,
            "fixture_sha256": self.config.fixture.sha256,
            "emitted_fixture_sha256": emitted.sha256,
            "emitted_fixture_onset": {
                "first_active_sample_8k": emitted.first_active_sample,
                "method": "first_pcm16_sample_abs_gte_threshold",
            },
            "decimation_rule": emitted.decimation_rule,
            "channel_sources": {
                "channel_a": "harness_rx",
                "channel_b": "harness_tx_reference",
            },
            "measured_media_format": {
                "encoding": "PCMU",
                "sample_rate": SAMPLE_RATE_8K,
                "channels": 1,
                "analysis_sample_rate": ANALYSIS_SAMPLE_RATE,
                "normalization": "g711_ulaw_zero_order_hold",
            },
            "target_legs": None,
            "detector_candidate": {
                "activity_threshold_dbfs": (
                    self.config.detector.activity_threshold_dbfs
                ),
                "silence_threshold_dbfs": (
                    self.config.detector.silence_threshold_dbfs
                ),
                "minimum_active_ms": self.config.detector.minimum_active_ms,
                "sustained_silence_ms": self.config.detector.sustained_silence_ms,
                "window_ms": self.config.detector.window_ms,
                "sample_rate": self.config.detector.sample_rate,
            },
            "tx_skew_bound_ms": TX_DRIFT_BOUND_NS // 1_000_000,
            "rtp_rx_counters": dict(self.rx_counters),
            "tx_delivery_counters": self.delivery_counters,
            "emission_boundary_tx_sample_8k": self.emission_boundary_tx_sample,
            "clock": "time.monotonic_ns",
        }
        if extra:
            manifest.update(extra)
        self.artifacts.write_json("manifest.json", manifest)
        self._event("teardown_recorded", now_ns, teardown_result=self.teardown_result)

    def _write_wav(self, name: str, payload: bytes, *, source: str) -> None:
        pcm16 = (
            b"".join(
                struct.pack(
                    "<h",
                    _PCMU_DECODE_TABLE[byte],
                )
                for byte in payload
            )
            if source == "pcmu"
            else payload
        )
        path = self.artifacts.path / name
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle, wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE_8K)
            wav.writeframes(pcm16[: MAX_RECORDED_SAMPLES_8K * 2])


def _build_pcmu_decode_table() -> tuple[int, ...]:
    values: list[int] = []
    for byte in range(256):
        complement = ~byte & 0xFF
        sign = complement & 0x80
        exponent = (complement >> 4) & 0x07
        mantissa = complement & 0x0F
        magnitude = ((mantissa << 3) + 0x84) << exponent
        magnitude -= 0x84
        values.append(-magnitude if sign else magnitude)
    return tuple(values)


_PCMU_DECODE_TABLE = _build_pcmu_decode_table()


def decode_pcmu_to_pcm16_8k(payload: bytes) -> bytes:
    """Plain G.711 expansion at 8 kHz (no zero-order hold)."""
    out = array("h", (0 for _ in range(len(payload))))
    for index, byte in enumerate(payload):
        out[index] = _PCMU_DECODE_TABLE[byte]
    return out.tobytes()


def write_emitted_fixture_wav(emitted: EmittedFixture, path: Path) -> None:
    """Persist the derived emission fixture for calibration review."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE_8K)
        wav.writeframes(emitted.pcm16_8k)


def _run_live_preflight() -> None:
    """Refuse dialing unless ignore, tests, lint, and types are clean."""
    import subprocess
    import sys

    from bench.acoustic_probe import ARTIFACT_ROOT, REPOSITORY_ROOT

    checks = (
        (
            ["git", "check-ignore", "-q", f"{ARTIFACT_ROOT}{os.sep}"],
            "artifact ignore rule",
        ),
        ([sys.executable, "-m", "pytest", "-q"], "offline tests"),
        ([sys.executable, "-m", "ruff", "check", "onset/", "bench/", "tests/"], "Ruff"),
        ([sys.executable, "-m", "mypy", "onset/", "bench/", "tests/"], "mypy"),
    )
    for command, label in checks:
        result = subprocess.run(command, check=False, cwd=REPOSITORY_ROOT)
        if result.returncode != 0:
            raise SystemExit(f"live preflight failed: {label}")


def main() -> None:
    import argparse
    import time
    from pathlib import Path as _Path

    from bench.acoustic_probe import (
        ARTIFACT_ROOT,
        load_fixture,
        validate_artifact_root,
    )
    from bench.media_capture import ArtifactDirectory, new_run_id

    parser = argparse.ArgumentParser(description="SIP media-endpoint harness")
    parser.add_argument(
        "--live", action="store_true", help="allow one bounded live attempt"
    )
    parser.add_argument("--fixture", type=_Path)
    arguments = parser.parse_args()
    if not arguments.live:
        print("offline safety gate: no call placed; run the offline test suite")
        return
    if os.environ.get("BENCH_LIVE") != "1":
        raise SystemExit("live gate closed: BENCH_LIVE=1 is also required")
    if arguments.fixture is None:
        raise SystemExit("--fixture is required for live mode")
    required = {
        name: os.environ.get(name, "")
        for name in (
            "BENCH_SIP_USERNAME",
            "BENCH_SIP_PASSWORD",
            "BENCH_SIP_DOMAIN",
            "BENCH_SIP_CALLER_ID",
            "BENCH_AGENT_NUMBER",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "missing required trusted live configuration: " + ", ".join(missing)
        )
    fixture = load_fixture(arguments.fixture)
    emitted = derive_emission_fixture(fixture)
    _run_live_preflight()
    artifacts = ArtifactDirectory(
        validate_artifact_root(ARTIFACT_ROOT), new_run_id()
    )
    write_emitted_fixture_wav(emitted, artifacts.path / "emitted_fixture_8k.wav")
    print(
        "live configuration accepted: one attempt, 60-second hard cap, "
        f"fixture_sha256={fixture.sha256}, emitted_sha256={emitted.sha256}"
    )
    from bench.sip_media_pjsua import run_live_call

    session = SipSession(
        SipSessionConfig(fixture=fixture, emitted=emitted),
        artifacts,
        dial_requested_ns=time.monotonic_ns(),
    )
    run_live_call(
        session,
        sip_username=required["BENCH_SIP_USERNAME"],
        sip_password=required["BENCH_SIP_PASSWORD"],
        sip_domain=required["BENCH_SIP_DOMAIN"],
        caller_id=required["BENCH_SIP_CALLER_ID"],
        agent_number=required["BENCH_AGENT_NUMBER"],
    )
    print(
        "terminal outcome: "
        f"{session.outcome} teardown={session.teardown_result} "
        f"artifacts={artifacts.path.name}"
    )


if __name__ == "__main__":
    main()
