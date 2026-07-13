"""Offline calibration cases for the Phase 2 acoustic-stop detector."""

from __future__ import annotations

import math
from array import array

import pytest

from bench.acoustic_stop import (
    DetectorConfig,
    LabeledCalibrationSegment,
    analyze_acoustic_stop,
    evaluate_bounded_calibration,
)


def pcm(*segments: tuple[int, int]) -> bytes:
    """Build (amplitude, milliseconds) segments at the detector sample rate."""
    values = array("h")
    for amplitude, milliseconds in segments:
        values.extend([amplitude] * (16 * milliseconds))
    return values.tobytes()


def test_all_silence_has_no_stop() -> None:
    analysis = analyze_acoustic_stop(pcm((0, 1_000)), DetectorConfig())
    assert analysis.result is None


def test_continuous_active_signal_has_no_stop() -> None:
    analysis = analyze_acoustic_stop(pcm((8_000, 1_000)), DetectorConfig())
    assert analysis.result is None


def test_active_then_sustained_silence_backdates_to_silence_start() -> None:
    config = DetectorConfig(minimum_active_ms=100, sustained_silence_ms=500)
    analysis = analyze_acoustic_stop(pcm((8_000, 200), (0, 500)), config)
    assert analysis.result is not None
    assert analysis.result.silence_start_sample == 3_200
    assert analysis.result.confirmation_sample == 11_200
    assert analysis.result.resolution_samples == 320


def test_short_pause_and_resumed_signal_do_not_stop() -> None:
    audio = pcm((8_000, 200), (0, 200), (8_000, 200))
    assert analyze_acoustic_stop(audio, DetectorConfig()).result is None


def test_natural_short_pauses_do_not_mask_later_real_stop() -> None:
    audio = pcm(
        (8_000, 120),
        (0, 80),
        (7_000, 120),
        (0, 160),
        (8_000, 120),
        (0, 500),
    )
    result = analyze_acoustic_stop(audio, DetectorConfig()).result
    assert result is not None
    assert result.silence_start_sample == 16 * 600


def test_abrupt_cut_is_detected() -> None:
    result = analyze_acoustic_stop(
        pcm((12_000, 200), (0, 500)), DetectorConfig()
    ).result
    assert result is not None


def test_fade_out_reaches_silence_without_perception_claim() -> None:
    values = array("h")
    for index in range(16 * 400):
        values.append(int(10_000 * (1 - index / (16 * 400))))
    values.extend([0] * (16 * 600))
    result = analyze_acoustic_stop(values.tobytes(), DetectorConfig()).result
    assert result is not None


def test_low_background_noise_after_speech_is_silence() -> None:
    result = analyze_acoustic_stop(
        pcm((8_000, 200), (50, 500)), DetectorConfig()
    ).result
    assert result is not None


def test_noise_above_silence_threshold_does_not_stop() -> None:
    config = DetectorConfig(
        activity_threshold_dbfs=-20.0,
        silence_threshold_dbfs=-45.0,
    )
    # 400 PCM units is about -38 dBFS: not active, but above the silence gate.
    analysis = analyze_acoustic_stop(pcm((8_000, 200), (400, 800)), config)
    assert analysis.result is None


def test_exact_hold_boundary() -> None:
    config = DetectorConfig(window_ms=20, sustained_silence_ms=500)
    too_short = analyze_acoustic_stop(pcm((8_000, 200), (0, 480)), config)
    exact = analyze_acoustic_stop(pcm((8_000, 200), (0, 500)), config)
    assert too_short.result is None
    assert exact.result is not None


def test_non_window_aligned_final_input_is_supported() -> None:
    audio = pcm((8_000, 200), (0, 510))[:-2]
    assert analyze_acoustic_stop(audio, DetectorConfig()).result is not None


def test_odd_byte_pcm_is_rejected() -> None:
    with pytest.raises(ValueError, match="odd-byte"):
        analyze_acoustic_stop(b"\x00", DetectorConfig())


def test_clipping_is_reported() -> None:
    result = analyze_acoustic_stop(
        pcm((32_767, 200), (0, 500)), DetectorConfig()
    ).result
    assert result is not None
    assert result.clipped_samples == 3_200


def test_minimum_signed_pcm_value_is_handled() -> None:
    analysis = analyze_acoustic_stop(pcm((-32_768, 200), (0, 500)), DetectorConfig())
    assert analysis.result is not None
    assert math.isfinite(analysis.windows[0].rms_dbfs)


def test_no_prior_active_interval_has_no_stop() -> None:
    audio = pcm((8_000, 80), (0, 800))
    assert (
        analyze_acoustic_stop(audio, DetectorConfig(minimum_active_ms=100)).result
        is None
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channels": 2},
        {"sample_width": 1},
        {"sustained_silence_ms": 99},
        {"sustained_silence_ms": 1_001},
    ],
)
def test_unsupported_detector_configuration_is_rejected(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        DetectorConfig(**kwargs)


def test_bounded_calibration_records_every_candidate_without_selecting() -> None:
    segments = (
        LabeledCalibrationSegment(
            "pause", "natural_pause", pcm((8_000, 200), (0, 100), (8_000, 200))
        ),
        LabeledCalibrationSegment(
            "forced", "forced_stop", pcm((8_000, 200), (0, 1_000))
        ),
    )
    records = evaluate_bounded_calibration(
        segments,
        activity_thresholds_dbfs=(-38.0,),
        silence_thresholds_dbfs=(-45.0,),
        hold_step_ms=100,
    )
    assert len(records) == 20
    assert {record.sustained_silence_ms for record in records} == set(
        range(100, 1_001, 100)
    )
    assert any(record.failure == "natural_pause_false_stop" for record in records)
    assert not hasattr(records, "selected_profile")


def test_bounded_calibration_records_invalid_declared_candidates() -> None:
    segment = LabeledCalibrationSegment(
        "forced", "forced_stop", pcm((8_000, 200), (0, 1_000))
    )
    records = evaluate_bounded_calibration(
        (segment,),
        activity_thresholds_dbfs=(-50.0,),
        silence_thresholds_dbfs=(-45.0,),
        statistics=("rms_dbfs", "unsupported"),
        window_sizes_ms=(20, 0),
    )
    assert {record.failure for record in records} >= {
        "invalid_threshold_order",
        "invalid_window",
        "unsupported_statistic",
    }
    assert all(not record.passed for record in records)
