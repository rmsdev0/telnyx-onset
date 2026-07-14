"""Pure PCM16 energy detector for a harness-side acoustic stop.

The detector locates a signal boundary in captured audio. It deliberately does
not claim to measure a human-perceived stop.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from typing import Literal

CALIBRATION_ACTIVITY_THRESHOLDS_DBFS = (-42.0, -40.0, -38.0, -36.0)
CALIBRATION_SILENCE_THRESHOLDS_DBFS = (-50.0, -48.0, -45.0, -42.0)
CALIBRATION_WINDOW_SIZES_MS = (20,)
CALIBRATION_HOLD_STEP_MS = 100


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Frozen candidate settings for one calibration evaluation."""

    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    window_ms: int = 20
    activity_threshold_dbfs: float = -38.0
    silence_threshold_dbfs: float = -45.0
    minimum_active_ms: int = 100
    sustained_silence_ms: int = 500

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels != 1:
            raise ValueError("only mono audio is supported")
        if self.sample_width != 2:
            raise ValueError("only PCM16 audio is supported")
        if self.window_ms <= 0:
            raise ValueError("window_ms must be positive")
        if self.minimum_active_ms <= 0:
            raise ValueError("minimum_active_ms must be positive")
        if not 100 <= self.sustained_silence_ms <= 1_000:
            raise ValueError("sustained_silence_ms must be between 100 and 1000")
        if self.activity_threshold_dbfs < self.silence_threshold_dbfs:
            raise ValueError("activity threshold must not be below silence threshold")

    @property
    def window_samples(self) -> int:
        return self.sample_rate * self.window_ms // 1_000


@dataclass(frozen=True, slots=True)
class EnergyWindow:
    start_sample: int
    sample_count: int
    rms_dbfs: float
    clipped_samples: int


@dataclass(frozen=True, slots=True)
class AcousticStopResult:
    """A confirmed signal stop, backdated to the first silent window."""

    silence_start_sample: int
    confirmation_sample: int
    resolution_samples: int
    clipped_samples: int


@dataclass(frozen=True, slots=True)
class DetectorAnalysis:
    windows: tuple[EnergyWindow, ...]
    result: AcousticStopResult | None
    malformed: bool = False


@dataclass(frozen=True, slots=True)
class LabeledCalibrationSegment:
    """A local synthetic/baseline segment excluded from benchmark results."""

    label: str
    kind: Literal["natural_pause", "silence", "forced_stop"]
    pcm16: bytes


@dataclass(frozen=True, slots=True)
class CalibrationCandidate:
    config: DetectorConfig | None
    statistic: str
    activity_threshold_dbfs: float
    silence_threshold_dbfs: float
    sustained_silence_ms: int
    window_ms: int
    segment: str
    expected_stop: bool
    detected_stop: bool
    passed: bool
    failure: str | None


def evaluate_bounded_calibration(
    segments: tuple[LabeledCalibrationSegment, ...],
    *,
    activity_thresholds_dbfs: tuple[float, ...],
    silence_thresholds_dbfs: tuple[float, ...],
    hold_step_ms: int = 100,
    statistics: tuple[str, ...] = ("rms_dbfs",),
    window_sizes_ms: tuple[int, ...] = (20,),
) -> tuple[CalibrationCandidate, ...]:
    """Evaluate every declared finite candidate without selecting a profile."""
    if not segments:
        raise ValueError("calibration segments are required")
    if not activity_thresholds_dbfs or not silence_thresholds_dbfs:
        raise ValueError("finite threshold sets are required")
    if not statistics or not window_sizes_ms:
        raise ValueError("finite statistic and window sets are required")
    if hold_step_ms <= 0 or 900 % hold_step_ms:
        raise ValueError("hold step must include both 100 and 1000 ms endpoints")
    holds = tuple(range(100, 1_001, hold_step_ms))
    records: list[CalibrationCandidate] = []
    for statistic in statistics:
        for window_ms in window_sizes_ms:
            for activity in activity_thresholds_dbfs:
                for silence in silence_thresholds_dbfs:
                    for hold in holds:
                        invalid: str | None = None
                        if statistic != "rms_dbfs":
                            invalid = "unsupported_statistic"
                        elif window_ms <= 0:
                            invalid = "invalid_window"
                        elif activity < silence:
                            invalid = "invalid_threshold_order"
                        config = (
                            None
                            if invalid
                            else DetectorConfig(
                                window_ms=window_ms,
                                activity_threshold_dbfs=activity,
                                silence_threshold_dbfs=silence,
                                sustained_silence_ms=hold,
                            )
                        )
                        for segment in segments:
                            detected = (
                                False
                                if config is None
                                else analyze_acoustic_stop(segment.pcm16, config).result
                                is not None
                            )
                            expected = segment.kind == "forced_stop"
                            passed = invalid is None and detected is expected
                            records.append(
                                CalibrationCandidate(
                                    config=config,
                                    statistic=statistic,
                                    activity_threshold_dbfs=activity,
                                    silence_threshold_dbfs=silence,
                                    sustained_silence_ms=hold,
                                    window_ms=window_ms,
                                    segment=segment.label,
                                    expected_stop=expected,
                                    detected_stop=detected,
                                    passed=passed,
                                    failure=invalid
                                    if invalid
                                    else (
                                        None
                                        if passed
                                        else (
                                            "natural_pause_false_stop"
                                            if detected
                                            else "forced_stop_missed"
                                        )
                                    ),
                                )
                            )
    if not records:
        raise ValueError("no valid detector candidates")
    return tuple(records)


def _pcm16_samples(pcm16: bytes) -> array[int]:
    if len(pcm16) % 2:
        raise ValueError("malformed odd-byte PCM16 input")
    samples = array("h")
    samples.frombytes(pcm16)
    return samples


def _rms_dbfs(samples: array[int], start: int, end: int) -> tuple[float, int]:
    if end <= start:
        return -math.inf, 0
    total = 0
    clipped = 0
    for sample in samples[start:end]:
        value = int(sample)
        total += value * value  # handles -32768 without abs overflow
        if value in (-32_768, 32_767):
            clipped += 1
    rms = math.sqrt(total / (end - start))
    if rms == 0:
        return -math.inf, clipped
    return 20.0 * math.log10(rms / 32_768.0), clipped


def analyze_acoustic_stop(pcm16: bytes, config: DetectorConfig) -> DetectorAnalysis:
    """Analyze PCM and return the first subsequently confirmed silence start.

    Activity must be continuous for ``minimum_active_ms`` before a stop can be
    armed. A pause shorter than ``sustained_silence_ms`` is tolerated because
    later activity resets the candidate silence interval.
    """
    samples = _pcm16_samples(pcm16)
    size = config.window_samples
    if size <= 0:
        raise ValueError("window resolves to zero samples")

    windows: list[EnergyWindow] = []
    active_samples = 0
    armed = False
    silence_start: int | None = None
    clipped_total = 0
    minimum_active_samples = config.sample_rate * config.minimum_active_ms // 1_000
    hold_samples = config.sample_rate * config.sustained_silence_ms // 1_000

    for start in range(0, len(samples), size):
        end = min(start + size, len(samples))
        dbfs, clipped = _rms_dbfs(samples, start, end)
        clipped_total += clipped
        windows.append(EnergyWindow(start, end - start, dbfs, clipped))

        if dbfs >= config.activity_threshold_dbfs:
            active_samples += end - start
            armed = armed or active_samples >= minimum_active_samples
            silence_start = None
            continue

        if not armed:
            active_samples = 0
            continue

        if dbfs <= config.silence_threshold_dbfs:
            if silence_start is None:
                silence_start = start
            if end - silence_start >= hold_samples:
                return DetectorAnalysis(
                    windows=tuple(windows),
                    result=AcousticStopResult(
                        silence_start_sample=silence_start,
                        confirmation_sample=end,
                        resolution_samples=size,
                        clipped_samples=clipped_total,
                    ),
                )
        else:
            silence_start = None

    return DetectorAnalysis(windows=tuple(windows), result=None)
