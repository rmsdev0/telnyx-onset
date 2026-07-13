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
import warnings
import wave
from array import array
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

with warnings.catch_warnings():
    # audioop is deprecated for 3.13 but is the only C-speed G.711 path in
    # the standard library; equivalence with the reviewed pure-Python
    # reference implementations is pinned by offline tests.
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

from bench.acoustic_stop import DetectorConfig

if TYPE_CHECKING:
    from pathlib import Path

    from bench.acoustic_probe import Fixture
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
# Burst bound on tx frame requests: the emission stamp equals the wire
# instant only while the stack's tick clock never batches catch-up pulls, so
# two pulls closer than this invalidate the handoff-equals-wire assumption.
# Late pulls remain wire-accurate and are recorded as telemetry instead.
TX_BURST_BOUND_NS = FRAME_MS * 1_000_000 // 2
# Declared bounds on tolerated, recorded rx concealment (Amendment 1
# addendum): beyond either bound the transport is unfit for measurement.
MAX_RX_VOID_EVENTS = 5
MAX_RX_VOID_TOTAL_NS = 1_000_000_000
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


def fast_encode_pcm16_to_pcmu(pcm16: bytes) -> bytes:
    """C-speed G.711 encode; equivalence with the reference is test-pinned."""
    return bytes(audioop.lin2ulaw(pcm16, 2))


def fast_decode_pcmu_8k_to_pcm16_16k(payload: bytes) -> bytes:
    """C-speed reviewed normalization: G.711 expand plus zero-order hold."""
    lin = array("h")
    lin.frombytes(audioop.ulaw2lin(payload, 2))
    doubled = array("h", bytes(len(lin) * 4))
    doubled[0::2] = lin
    doubled[1::2] = lin
    return doubled.tobytes()


# Each decoded rx frame is exactly one 20 ms detector window (320 samples at
# the 16 kHz analysis rate), so the incremental state machine below operates
# on the per-frame RMS values recorded at ingest. Live attempt 1 (SIP) proved
# that re-scanning raw PCM per frame starves the stack's media clock; the
# window rules themselves (sustained-activity arming per the attempt-6 guard,
# backdated stop, hold durations) are unchanged from analyze_acoustic_stop.


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
        # windows (incremental 20 ms detector state; one rx frame == one window)
        self.anchor_ns: int | None = None
        self.anchor_sample_16k: int | None = None
        self.greeting_stop_ns: int | None = None
        self._greeting_stop_sample_16k: int | None = None
        self._scan_index = 0
        self._run_start_window: int | None = None
        self._run_windows = 0
        self._silence_start_window: int | None = None
        self._d_run_start_window: int | None = None
        self._d_run_windows = 0
        self._d_candidate_start_window: int | None = None
        self._echo_snapshot: bytes | None = None
        self._cadence_gate_active = True
        self._pending_metadata: list[dict[str, object]] = []
        self._last_pull_ns: int | None = None
        self.tx_pull_max_late_ns = 0
        self.tx_pull_min_interval_ns: int | None = None
        # Loss-void machinery (Amendment 1 addendum): the stack conceals
        # lost packets before the port surface, so stream-stat loss is
        # localized to a reported interval whose windows become "unknown" —
        # they can certify neither activity nor silence, and every
        # certification run resets across them. The scan watermark holds
        # analysis back until each interval's loss verdict has arrived.
        self._rx_voids: list[tuple[int, int]] = []
        self.rx_void_total_ns = 0
        self._scan_watermark_ns: int | None = None
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

    def _flush_metadata(self) -> None:
        if self._pending_metadata:
            rows = self._pending_metadata
            self._pending_metadata = []
            self.artifacts.append_jsonl_rows("frame_metadata.jsonl", rows)

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
        pcm16 = fast_decode_pcmu_8k_to_pcm16_16k(frame.pcmu)
        self._rx_frames.append((len(self._rx_16k), frame.host_receive_monotonic_ns))
        self._rx_pcmu.extend(frame.pcmu)
        self._rx_16k.extend(pcm16)
        rms = self._frame_rms_dbfs(pcm16)
        self._rx_frame_rms.append((frame.host_receive_monotonic_ns, rms))
        # Metadata rows are buffered and flushed in batches off the per-frame
        # path: SIP attempt 2 proved per-frame file opens stall the media
        # clock. Order and append-only discipline are unchanged.
        self._pending_metadata.append(
            {
                "track": "rx",
                "rtp_sequence": frame.rtp_sequence,
                "rtp_timestamp": frame.rtp_timestamp,
                "host_receive_monotonic_ns": frame.host_receive_monotonic_ns,
                "rms_dbfs": None if rms == -math.inf else round(rms, 2),
                "payload_bytes": len(pcm16),
                "source_payload_bytes": len(frame.pcmu),
            }
        )
        self._rx_frame_count += 1
        self._check_stimulus_overlap(frame.host_receive_monotonic_ns, rms)

    def pull_tx_frame(self, now_ns: int) -> bytes:
        """Media-clock frame request; returns one 160-byte PCMU payload."""
        if self._tx_start_ns is None:
            self._tx_start_ns = now_ns
        ideal_ns = self._tx_start_ns + self._tx_index * FRAME_MS * 1_000_000
        self.tx_pull_max_late_ns = max(self.tx_pull_max_late_ns, now_ns - ideal_ns)
        if self._last_pull_ns is not None:
            interval = now_ns - self._last_pull_ns
            if self.tx_pull_min_interval_ns is None:
                self.tx_pull_min_interval_ns = interval
            else:
                self.tx_pull_min_interval_ns = min(
                    self.tx_pull_min_interval_ns, interval
                )
            # A burst pull means the stack is batching catch-up frames and a
            # handoff stamp would no longer equal the wire instant; that is
            # the condition that corrupts the emission boundary. Late pulls
            # remain wire-accurate and are recorded as telemetry only. The
            # gate matters only until separating silence is confirmed: tx is
            # constant silence afterward.
            if (
                interval < TX_BURST_BOUND_NS
                and self._cadence_gate_active
                and self.outcome is None
            ):
                self._event(
                    "tx_pull_burst",
                    now_ns,
                    interval_ns=interval,
                    frame_index=self._tx_index,
                )
                self._fail("media_ordering_anomaly", now_ns)
        self._last_pull_ns = now_ns
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
        """Analysis and deadline enforcement, called off the media path."""
        if len(self._pending_metadata) >= 25:
            self._flush_metadata()
        if self.outcome is not None:
            return
        if self.answered_ns is not None and self._rx_frames:
            self._analyze(now_ns)
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

    def set_scan_watermark(self, watermark_ns: int) -> None:
        """Analysis may consume rx windows at or before this host time.

        The media layer advances the watermark after each stream-stat poll,
        so a window is only certified once its poll interval's loss verdict
        is known. Offline drivers that report loss synchronously may leave
        the watermark unset.
        """
        self._scan_watermark_ns = watermark_ns

    def report_rx_loss(
        self, packets: int, interval_start_ns: int, now_ns: int
    ) -> None:
        """Stream-stat loss localized to the last poll interval.

        The stack conceals lost packets before the port surface, so the
        concealed audio lies somewhere in [interval_start_ns, now_ns]. That
        interval is voided: its windows can certify neither activity nor
        silence, and every certification run resets across it. Bounded and
        recorded; beyond the declared bounds the transport is unfit and the
        run fails closed.
        """
        if packets <= 0:
            return
        self.rx_counters["reported_loss"] = (
            int(self.rx_counters.get("reported_loss", 0)) + packets
        )
        self._rx_voids.append((interval_start_ns, now_ns))
        self.rx_void_total_ns += max(0, now_ns - interval_start_ns)
        self._event(
            "rx_loss_interval_voided",
            now_ns,
            packets=packets,
            interval_start_ns=interval_start_ns,
        )
        if (
            len(self._rx_voids) > MAX_RX_VOID_EVENTS
            or self.rx_void_total_ns > MAX_RX_VOID_TOTAL_NS
        ):
            self._fail("rx_timeline_discontinuity", now_ns)

    def _in_void(self, host_ns: int) -> bool:
        return any(start <= host_ns <= end for start, end in self._rx_voids)

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
        total = math.sumprod(samples, samples)
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
            self.emission_boundary_ns is None
            or rms_dbfs < self.config.detector.activity_threshold_dbfs
            or host_ns < self.emission_boundary_ns
        ):
            return
        # An active rx frame while the fixture is still on the wire
        # (fixture_end_ns unset) is overlap just as much as one inside the
        # completed transmission interval. The interval is half-open: a frame
        # timestamped exactly at fixture_end carries post-transmission audio.
        if self.fixture_end_ns is None or host_ns < self.fixture_end_ns:
            self._fail("stimulus_overlap", host_ns)

    def _analyze(self, now_ns: int) -> None:
        """Advance the incremental window state machine over new rx frames."""
        detector = self.config.detector
        windows_per_hold = detector.sustained_silence_ms // FRAME_MS
        windows_to_arm = detector.minimum_active_ms // FRAME_MS
        while self._scan_index < len(self._rx_frame_rms):
            index = self._scan_index
            host_ns, rms = self._rx_frame_rms[index]
            if (
                self._scan_watermark_ns is not None
                and host_ns > self._scan_watermark_ns
            ):
                break
            self._scan_index += 1
            if self._in_void(host_ns):
                # Unknown window: certifies neither activity nor silence,
                # and every certification run resets across it.
                self._run_start_window = None
                self._run_windows = 0
                self._silence_start_window = None
                self._d_run_start_window = None
                self._d_run_windows = 0
                continue
            active = rms >= detector.activity_threshold_dbfs
            silent = rms <= detector.silence_threshold_dbfs
            if self.anchor_ns is None:
                if active:
                    if self._run_start_window is None:
                        self._run_start_window = index
                    self._run_windows += 1
                    if self._run_windows >= windows_to_arm:
                        self.anchor_sample_16k = self._run_start_window * 320
                        self.anchor_ns = self._rx_frame_rms[
                            self._run_start_window
                        ][0]
                        self._event(
                            "window_a_anchor",
                            self.anchor_ns,
                            sample_16k=self.anchor_sample_16k,
                        )
                else:
                    self._run_start_window = None
                    self._run_windows = 0
                continue
            if self.greeting_stop_ns is None:
                if active:
                    self._silence_start_window = None
                elif silent:
                    if self._silence_start_window is None:
                        self._silence_start_window = index
                    if index - self._silence_start_window + 1 >= windows_per_hold:
                        stop_sample = self._silence_start_window * 320
                        self._greeting_stop_sample_16k = stop_sample
                        self.greeting_stop_ns = self._rx_frame_rms[
                            self._silence_start_window
                        ][0]
                        self._event(
                            "agent_natural_stop_confirmed",
                            now_ns,
                            backdated_sample_16k=stop_sample,
                        )
                        self._fixture_armed = True
                        self._event("stimulus_armed", now_ns)
                else:
                    self._silence_start_window = None
                continue
            if self._window_d_open_sample_16k is None:
                continue
            if host_ns < (self._separation_confirmed_ns or 0):
                continue
            if self._echo_snapshot is not None or self.outcome is not None:
                continue
            if active:
                if self._d_run_start_window is None:
                    self._d_run_start_window = index
                self._d_run_windows += 1
                if (
                    self._d_run_windows >= windows_to_arm
                    and self._d_candidate_start_window is None
                ):
                    # First confirmed post-boundary activity: freeze its onset
                    # as the sticky echo-alignment anchor. Later speech pauses
                    # move the run counter but must not move this anchor, or
                    # the fixture-length judging window can never fill.
                    self._d_candidate_start_window = self._d_run_start_window
                    self._event("window_d_candidate", now_ns)
            else:
                self._d_run_start_window = None
                self._d_run_windows = 0
        if (
            self.fixture_end_ns is not None
            and self._separation_confirmed_ns is None
        ):
            self._evaluate_separating_silence(now_ns)
        if self._d_candidate_start_window is not None:
            self._prepare_echo_snapshot(now_ns)

    def _prepare_echo_snapshot(self, now_ns: int) -> None:
        """Snapshot Window D audio once enough exists to judge an echo.

        The correlation itself is expensive and runs OUTSIDE the media path:
        the driving loop collects the snapshot via pending_echo_check() and
        returns the verdict via apply_echo_verdict(). The snapshot begins at
        the sticky first-activity anchor and is withheld until it spans the
        full fixture plus the alignment search window, so a delayed echo
        cannot arrive after a premature completion.
        """
        assert self._d_candidate_start_window is not None
        anchor_byte = self._d_candidate_start_window * 640
        search_bytes = self.config.echo_search_ms * (ANALYSIS_SAMPLE_RATE // 1_000) * 2
        required_end = anchor_byte + len(self.config.fixture.pcm16) + search_bytes
        if len(self._rx_16k) < required_end:
            return
        self._echo_snapshot = bytes(self._rx_16k[anchor_byte:])

    def pending_echo_check(self) -> bytes | None:
        """Window D audio awaiting the off-path fixture correlation."""
        return self._echo_snapshot if self.outcome is None else None

    def apply_echo_verdict(self, match: object | None, now_ns: int) -> None:
        if self.outcome is not None or self._echo_snapshot is None:
            return
        self._echo_snapshot = None
        if match is not None:
            self._fail("post_stimulus_echo_detected", now_ns)
            return
        self.outcome = "capture_complete_pending_review"
        self._event("capture_completed", now_ns)

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
            if (
                self._scan_watermark_ns is not None
                and host > self._scan_watermark_ns
            ):
                break
            if self._in_void(host):
                run = []
                continue
            if rms <= self.config.detector.silence_threshold_dbfs:
                run.append(host)
                if len(run) * FRAME_MS >= needed_ms:
                    boundary_ns = run[0] + self.config.separating_silence_ns
                    self._separation_confirmed_ns = boundary_ns
                    self._window_d_open_sample_16k = (
                        self._first_sample_at_or_after(boundary_ns)
                    )
                    # tx is constant silence from here on; drift can no
                    # longer corrupt a measured boundary.
                    self._cadence_gate_active = False
                    self._event(
                        "separating_silence_confirmed",
                        now_ns,
                        boundary_host_ns=boundary_ns,
                    )
                    return
            else:
                run = []

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
        self._flush_metadata()
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
            "tx_burst_bound_ms": TX_BURST_BOUND_NS // 1_000_000,
            "tx_pull_max_late_ms": self.tx_pull_max_late_ns // 1_000_000,
            "tx_pull_min_interval_ms": (
                None
                if self.tx_pull_min_interval_ns is None
                else self.tx_pull_min_interval_ns / 1_000_000
            ),
            "rtp_rx_counters": dict(self.rx_counters),
            "rx_void_events": len(self._rx_voids),
            "rx_void_total_ms": self.rx_void_total_ns // 1_000_000,
            "rx_void_bounds": {
                "max_events": MAX_RX_VOID_EVENTS,
                "max_total_ms": MAX_RX_VOID_TOTAL_NS // 1_000_000,
            },
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
        f"artifacts={artifacts.path.name}",
        flush=True,
    )
    # Artifacts are flushed and the endpoint was destroyed best-effort; exit
    # hard so pjsua2 wrapper destructor ordering can never abort the process
    # after a completed run (observed on the first live attempt).
    os._exit(0)


if __name__ == "__main__":
    main()
