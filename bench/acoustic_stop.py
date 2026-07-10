"""Pure PCM16 energy detector for a harness-side acoustic stop.

The detector locates a signal boundary in captured audio. It deliberately does
not claim to measure a human-perceived stop.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass


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
