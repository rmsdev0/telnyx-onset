"""Offline validation of the SIP media-endpoint harness core (spec §12)."""

from __future__ import annotations

import json
import math
import wave
from array import array
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bench.acoustic_probe import (
    Fixture,
    decode_pcmu_8k_to_pcm16_16k,
    match_fixture_reference,
)
from bench.media_capture import ArtifactDirectory, new_run_id
from bench.sip_harness import (
    FRAME_MS,
    PCMU_FRAME_BYTES,
    EmittedFixture,
    RxFrame,
    SipSession,
    SipSessionConfig,
    derive_emission_fixture,
    encode_pcm16_to_pcmu,
)

NS_PER_FRAME = FRAME_MS * 1_000_000


def _fixture() -> Fixture:
    frames = tuple(
        array("h", [amplitude] * 320).tobytes()
        for amplitude in (500, 2_000, 8_000, 1_000)
    )
    return Fixture(
        pcm16=b"".join(frames),
        sha256="a" * 64,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        frame_bytes=640,
        first_active_sample=0,
        onset_method="test",
        onset_threshold=1,
    )


def _session(tmp_path: Path) -> SipSession:
    fixture = _fixture()
    config = SipSessionConfig(
        fixture=fixture, emitted=derive_emission_fixture(fixture)
    )
    artifacts = ArtifactDirectory(tmp_path, new_run_id())
    return SipSession(config, artifacts, dial_requested_ns=1_000)


class Driver:
    """Deterministic fake media layer driving the session on a fake clock."""

    def __init__(self, session: SipSession, start_ns: int = 10_000) -> None:
        self.session = session
        self.now_ns = start_ns
        self.rtp_sequence = 100
        self.rtp_timestamp = 0
        self.tx_frames: list[bytes] = []

    def answer(self, codec: str = "PCMU") -> None:
        self.session.handle_answered(codec, self.now_ns)

    def exchange(
        self,
        rx_pcm16_8k: bytes | None = None,
        *,
        sequence_jump: int = 0,
    ) -> None:
        """Advance one 20 ms frame: pull tx, deliver rx, tick."""
        self.tx_frames.append(self.session.pull_tx_frame(self.now_ns))
        self.rtp_sequence += 1 + sequence_jump
        self.rtp_timestamp += PCMU_FRAME_BYTES * (1 + sequence_jump)
        pcmu = encode_pcm16_to_pcmu(rx_pcm16_8k or bytes(320))
        self.session.handle_rx_frame(
            RxFrame(
                pcmu=pcmu,
                rtp_sequence=self.rtp_sequence,
                rtp_timestamp=self.rtp_timestamp,
                host_receive_monotonic_ns=self.now_ns,
            )
        )
        self.session.tick(self.now_ns)
        self.now_ns += NS_PER_FRAME

    def run(self, frames: int, rx_pcm16_8k: bytes | None = None) -> None:
        for _ in range(frames):
            if self.session.outcome is not None:
                return
            self.exchange(rx_pcm16_8k)


LOUD_8K = array("h", [8_000] * 160).tobytes()
QUIET_8K = bytes(320)


def _echo_frames(emitted: EmittedFixture) -> list[bytes]:
    return [
        emitted.pcm16_8k[start : start + 320]
        for start in range(0, len(emitted.pcm16_8k), 320)
    ]


def test_mu_law_encoder_round_trips_against_reviewed_decoder() -> None:
    amplitudes = [0, 1, 8, 96, 500, 2_000, 8_000, 20_000, 32_000, -32_768]
    pcm = array("h", [max(-32_768, min(32_767, a)) for a in amplitudes]).tobytes()
    decoded_16k = decode_pcmu_8k_to_pcm16_16k(encode_pcm16_to_pcmu(pcm))
    decoded = array("h")
    decoded.frombytes(decoded_16k)
    for index, original in enumerate(amplitudes):
        value = decoded[index * 2]  # zero-order hold duplicates each sample
        tolerance = max(16, abs(original) * 0.06)
        assert abs(value - original) <= tolerance, (original, value)


def test_derived_fixture_is_frozen_hashed_and_onset_recomputed() -> None:
    fixture = _fixture()
    emitted = derive_emission_fixture(fixture)
    again = derive_emission_fixture(fixture)
    assert emitted.sha256 == again.sha256
    assert emitted.decimation_rule.startswith("hamming63_lowpass3400")
    assert all(len(frame) == PCMU_FRAME_BYTES for frame in emitted.pcmu_frames)
    # Onset preserved within one 20 ms frame of the canonical onset (0).
    assert emitted.first_active_sample < 160
    # The transmitted rendition still matches the canonical fixture envelope.
    zoh = decode_pcmu_8k_to_pcm16_16k(b"".join(emitted.pcmu_frames))
    match = match_fixture_reference(zoh, fixture)
    assert match is not None
    assert match.energy_envelope_correlation >= 0.85


def test_happy_path_completes_pending_review(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)  # greeting
    driver.run(30, QUIET_8K)  # natural stop (500 ms) + margin
    assert session.greeting_stop_ns is not None
    fixture_frames = len(session.config.emitted.pcmu_frames)
    driver.run(fixture_frames + 10, QUIET_8K)  # fixture tx + separating silence
    assert session.emission_boundary_ns is not None
    assert session.fixture_end_ns is not None
    driver.run(10, LOUD_8K)  # agent response
    driver.run(300, QUIET_8K)  # accumulate echo-gate audio, then complete
    assert session.outcome == "capture_complete_pending_review"
    session.finalize(teardown_result="hangup_sent", now_ns=driver.now_ns)
    manifest = json.loads((session.artifacts.path / "manifest.json").read_text())
    assert manifest["gate_outcome"] == "CAPTURE_COMPLETE_PENDING_REVIEW"
    assert manifest["emitted_fixture_sha256"] == session.config.emitted.sha256
    assert manifest["target_legs"] is None
    # The fixture actually went out on the tx wire path.
    assert any(f == session.config.emitted.pcmu_frames[0] for f in driver.tx_frames)
    with wave.open(str(session.artifacts.path / "tx_8k.wav")) as wav:
        assert wav.getframerate() == 8_000
        assert wav.getnframes() > 0


def test_setup_blip_cannot_anchor_window_a(tmp_path: Path) -> None:
    """The live-attempt-6 false-anchor failure mode must not recur."""
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(2, LOUD_8K)  # 40 ms blip: below the 100 ms sustained-arm rule
    driver.run(20, QUIET_8K)
    assert session.anchor_ns is None
    blip_end_ns = driver.now_ns
    driver.run(10, LOUD_8K)  # the real greeting
    assert session.anchor_ns is not None
    assert session.anchor_ns >= blip_end_ns


def test_no_activity_times_out_as_agent_audio_not_observed(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(800, QUIET_8K)  # 16 s of silence > 15 s horizon
    assert session.outcome == "agent_audio_not_observed"


def test_activity_without_stop_times_out_as_natural_stop_not_observed(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(800, LOUD_8K)
    assert session.outcome == "natural_stop_not_observed"


def test_rx_activity_during_fixture_is_stimulus_overlap(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)
    driver.run(30, QUIET_8K)
    assert session.greeting_stop_ns is not None
    driver.run(2, QUIET_8K)  # fixture transmission begins
    assert session.emission_boundary_ns is not None
    driver.run(3, LOUD_8K)  # rx activity while the fixture is on the wire
    assert session.outcome == "stimulus_overlap"


def test_continuous_activity_after_fixture_is_boundary_ambiguous(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)
    driver.run(30, QUIET_8K)
    fixture_frames = len(session.config.emitted.pcmu_frames)
    driver.run(fixture_frames + 2, QUIET_8K)
    assert session.fixture_end_ns is not None
    driver.run(900, LOUD_8K)  # never 100 ms of silence
    assert session.outcome == "stimulus_boundary_ambiguous"


def test_fixture_echo_after_boundary_fails_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)
    driver.run(30, QUIET_8K)
    fixture_frames = len(session.config.emitted.pcmu_frames)
    driver.run(fixture_frames + 10, QUIET_8K)
    assert session.emission_boundary_ns is not None
    # A delayed echo returns; repeated so it clears the 100 ms sustained-
    # activity arm (a single pass of this short test fixture is only 80 ms).
    for _ in range(2):
        for frame in _echo_frames(session.config.emitted):
            driver.exchange(frame)
    driver.run(400, QUIET_8K)
    assert session.outcome == "post_stimulus_echo_detected"


def test_rtp_gap_after_anchor_is_timeline_discontinuity(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)
    assert session.anchor_ns is not None
    driver.exchange(QUIET_8K, sequence_jump=3)
    assert session.outcome == "rx_timeline_discontinuity"


def test_rtp_gap_before_anchor_is_logged_not_fatal(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.exchange(QUIET_8K)
    driver.exchange(QUIET_8K, sequence_jump=2)
    assert session.outcome is None
    assert session.rx_counters["sequence_gaps"] == 1


def test_remote_bye_mid_window_is_call_hangup_with_teardown(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(5, LOUD_8K)
    session.handle_remote_bye(driver.now_ns)
    assert session.outcome == "call_hangup"
    session.finalize(teardown_result="hangup_sent", now_ns=driver.now_ns)
    manifest = json.loads((session.artifacts.path / "manifest.json").read_text())
    assert manifest["teardown_result"] == "remote_bye"
    assert manifest["failure_category"] == "call_hangup"


def test_non_pcmu_answer_and_renegotiation_fail_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.handle_answered("G722", 10_000)
    assert session.outcome == "media_format_mismatch"

    second = _session(tmp_path)
    second.handle_answered("PCMU", 10_000)
    second.handle_renegotiation(changed=False, now_ns=20_000)
    assert second.outcome is None
    second.handle_renegotiation(changed=True, now_ns=30_000)
    assert second.outcome == "media_format_mismatch"


def test_tx_cadence_drift_beyond_half_frame_fails_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.handle_answered("PCMU", 10_000)
    session.pull_tx_frame(10_000)
    session.pull_tx_frame(10_000 + NS_PER_FRAME)
    session.pull_tx_frame(10_000 + 2 * NS_PER_FRAME + 11_000_000)  # +11 ms
    assert session.outcome == "media_ordering_anomaly"


def test_cross_channel_predicates_use_host_time_not_sample_index(
    tmp_path: Path,
) -> None:
    """A skewed rx delivery clock must not fake or hide an overlap."""
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)
    driver.run(30, QUIET_8K)
    assert session.greeting_stop_ns is not None
    fixture_frames = len(session.config.emitted.pcmu_frames)
    driver.run(fixture_frames + 2, QUIET_8K)
    assert session.emission_boundary_ns is not None
    # Deliver a loud rx frame whose HOST time is after fixture end even
    # though its sample index falls inside the transmission span.
    assert session.fixture_end_ns is not None
    late = RxFrame(
        pcmu=encode_pcm16_to_pcmu(LOUD_8K),
        rtp_sequence=driver.rtp_sequence + 1,
        rtp_timestamp=driver.rtp_timestamp + PCMU_FRAME_BYTES,
        host_receive_monotonic_ns=session.fixture_end_ns + 1_000_000,
    )
    session.handle_rx_frame(late)
    assert session.outcome != "stimulus_overlap"


def test_early_media_is_excluded_from_the_measured_timeline(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    session.handle_rx_frame(
        RxFrame(
            pcmu=encode_pcm16_to_pcmu(LOUD_8K),
            rtp_sequence=1,
            rtp_timestamp=0,
            host_receive_monotonic_ns=5_000,
        )
    )
    assert session.anchor_ns is None
    events = (session.artifacts.path / "events.jsonl").read_text()
    assert '"early_media_excluded"' in events


def test_hard_cap_classifies_by_stage(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(10, LOUD_8K)
    session.tick(session.dial_requested_ns + 61 * 1_000_000_000)
    assert session.outcome == "natural_stop_not_observed"


def test_artifacts_contain_no_configured_identifiers(tmp_path: Path) -> None:
    session = _session(tmp_path)
    driver = Driver(session)
    driver.answer()
    driver.run(5, LOUD_8K)
    session.handle_remote_bye(driver.now_ns)
    session.finalize(teardown_result="hangup_sent", now_ns=driver.now_ns)
    text = "".join(
        path.read_text()
        for path in session.artifacts.path.iterdir()
        if path.suffix in {".json", ".jsonl"}
    )
    for secret in ("+1555", "sip.telnyx.com", "BENCH_SIP", "password"):
        assert secret not in text


def test_rms_of_silence_is_negative_infinity_safe() -> None:
    assert SipSession._frame_rms_dbfs(bytes(640)) == -math.inf


def test_answer_timeout_fires_before_answer(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.tick(session.dial_requested_ns + 16 * 1_000_000_000)
    assert session.outcome == "answer_timeout"


def test_no_rx_after_answer_is_stream_start_failed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.handle_answered("PCMU", 10_000)
    session.tick(10_000 + 16 * 1_000_000_000)
    assert session.outcome == "stream_start_failed"


@pytest.mark.parametrize("bad_bytes", [80, 172])
def test_wrong_rx_frame_size_fails_closed(tmp_path: Path, bad_bytes: int) -> None:
    session = _session(tmp_path)
    session.handle_answered("PCMU", 10_000)
    session.handle_rx_frame(
        RxFrame(
            pcmu=b"\xff" * bad_bytes,
            rtp_sequence=1,
            rtp_timestamp=0,
            host_receive_monotonic_ns=20_000,
        )
    )
    assert session.outcome == "media_format_mismatch"
