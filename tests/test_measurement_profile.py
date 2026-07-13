from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bench.acoustic_probe import MIN_FIXTURE_CORRELATION
from bench.acoustic_stop import (
    CALIBRATION_ACTIVITY_THRESHOLDS_DBFS,
    CALIBRATION_SILENCE_THRESHOLDS_DBFS,
    CALIBRATION_WINDOW_SIZES_MS,
    DetectorConfig,
)
from bench.sip_harness import (
    DECIMATION_RULE,
    FRAME_MS,
    HARD_CALL_CAP_NS,
    MAX_RECORDED_SAMPLES_8K,
    MAX_RX_VOID_EVENTS,
    MAX_RX_VOID_TOTAL_NS,
    TX_BURST_BOUND_NS,
    WATERMARK_STALL_BOUND_NS,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "bench" / "measurement_profile.json"


def test_frozen_measurement_profile_matches_reviewed_invariants() -> None:
    profile = json.loads(PROFILE_PATH.read_text())
    assert profile["status"] == "frozen"
    assert profile["phase_2_verdict"] == "GO"

    detector = profile["detector"]
    config = DetectorConfig(
        window_ms=detector["window_ms"],
        activity_threshold_dbfs=detector["activity_threshold_dbfs"],
        silence_threshold_dbfs=detector["silence_threshold_dbfs"],
        minimum_active_ms=detector["minimum_active_ms"],
        sustained_silence_ms=detector["sustained_silence_ms"],
    )
    assert config.window_ms in CALIBRATION_WINDOW_SIZES_MS
    assert config.activity_threshold_dbfs in CALIBRATION_ACTIVITY_THRESHOLDS_DBFS
    assert config.silence_threshold_dbfs in CALIBRATION_SILENCE_THRESHOLDS_DBFS

    frozen_fixture = profile["fixture"]
    for key in ("canonical_sha256", "emitted_sha256"):
        assert len(frozen_fixture[key]) == hashlib.sha256().digest_size * 2
        int(frozen_fixture[key], 16)
    assert frozen_fixture["required_tx_packets"] * 160 == (
        frozen_fixture["required_tx_bytes"]
    )

    assert profile["echo_gate"]["fixture_envelope_correlation_threshold"] == (
        MIN_FIXTURE_CORRELATION
    )
    assert profile["media_format"]["frame_ms"] == FRAME_MS
    assert profile["media_format"]["fixture_decimation_rule"] == DECIMATION_RULE
    assert profile["delivery_gate"]["tx_burst_bound_ms"] == (
        TX_BURST_BOUND_NS // 1_000_000
    )
    assert profile["loss_void_policy"] == {
        "maximum_events": MAX_RX_VOID_EVENTS,
        "maximum_total_ms": MAX_RX_VOID_TOTAL_NS // 1_000_000,
        "jitter_buffer_ms": 60,
        "void_end_pad_ms": 80,
        "watermark_stall_bound_ms": WATERMARK_STALL_BOUND_NS // 1_000_000,
    }
    assert profile["capture_limits"] == {
        "hard_call_cap_s": HARD_CALL_CAP_NS // 1_000_000_000,
        "maximum_recorded_samples_8k": MAX_RECORDED_SAMPLES_8K,
    }


def test_frozen_calibration_evidence_hash_is_well_formed() -> None:
    profile = json.loads(PROFILE_PATH.read_text())
    digest = profile["evidence"]["calibration_evidence_sha256"]
    assert len(digest) == hashlib.sha256().digest_size * 2
    int(digest, 16)
