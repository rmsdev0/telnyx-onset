"""Fail-closed Phase 3 configuration and sanitized milestone recording.

The benchmark sink records metadata only: no audio, transcript text, phone
number, provider call id, or secrets. Timestamps use one process-local
monotonic clock and carry a clock identifier; harness timestamps remain a
separate clock family.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from onset.types import BenchmarkMode

if TYPE_CHECKING:
    from onset.settings import Settings


STRICT_MODES = frozenset(
    {BenchmarkMode.ONSET_FD_VAD, BenchmarkMode.ONSET_FD_TRANSCRIPT}
)
SPEECH_BEARING_RULE_VERSION = "frozen-rms-frame-v1"


class MilestoneSink(Protocol):
    """Small synchronous sink used only from the agent event loop."""

    def record(
        self, event: str, *, monotonic_ns: int | None = None, **fields: object
    ) -> None: ...


class NullMilestoneSink:
    def record(
        self, event: str, *, monotonic_ns: int | None = None, **fields: object
    ) -> None:
        return


@dataclass(frozen=True, slots=True)
class FrozenRuntimeProfile:
    path: Path
    sha256: str
    activity_threshold_dbfs: float
    sample_rate: int
    frame_ms: int


class JsonlMilestoneSink:
    """Append immutable, sanitized agent-local milestone rows."""

    def __init__(
        self,
        path: Path,
        *,
        trial_id: str,
        condition: BenchmarkMode,
        profile_sha256: str,
    ) -> None:
        self._path = path
        self._trial_id = trial_id
        self._condition = condition.value
        self._profile_sha256 = profile_sha256
        self._clock_id = (
            f"agent:{platform.node() or 'unknown'}:{os.getpid()}:monotonic_ns"
        )
        self._created = False
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, event: str, *, monotonic_ns: int | None = None, **fields: object
    ) -> None:
        row: dict[str, object] = {
            "schema_version": 1,
            "event": event,
            "trial_id": self._trial_id,
            "condition": self._condition,
            "clock_id": self._clock_id,
            "process_id": os.getpid(),
            "monotonic_ns": monotonic_ns
            if monotonic_ns is not None
            else time.monotonic_ns(),
            "profile_sha256": self._profile_sha256,
        }
        row.update(fields)
        flags = os.O_WRONLY | os.O_NOFOLLOW
        flags |= os.O_APPEND if self._created else os.O_CREAT | os.O_EXCL
        fd = os.open(self._path, flags, 0o600)
        self._created = True
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()


def matched_runtime_config(settings: Settings) -> dict[str, object]:
    """Return the non-secret settings that must match across primary modes."""
    names = (
        "media_codec",
        "sample_rate",
        "frame_ms",
        "stream_bidirectional_target_legs",
        "llm_base_url",
        "llm_model",
        "llm_temperature",
        "llm_max_tokens",
        "stt_engine",
        "stt_input_format",
        "stt_language",
        "stt_max_reconnects",
        "stt_reconnect_backoff_s",
        "stt_feed_max_frames",
        "tts_voice",
        "tts_streaming_decode",
        "tts_prebuffer_ms",
        "vad_aggressiveness",
        "vad_speech_onset_ms",
        "vad_silence_rearm_ms",
        "inject_lead_frames",
        "half_duplex",
        "listen_guard_ms",
    )
    return {name: getattr(settings, name) for name in names}


def load_frozen_runtime_profile(
    path: str | Path, settings: Settings
) -> FrozenRuntimeProfile:
    """Load the reviewed profile and reject runtime/profile mismatches."""
    profile_path = Path(path)
    try:
        value = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"cannot load benchmark profile: {e}") from e
    if not isinstance(value, dict) or value.get("status") != "frozen":
        raise ValueError("benchmark profile must have status=frozen")
    if value.get("phase_2_verdict") != "GO":
        raise ValueError("benchmark profile must carry phase_2_verdict=GO")
    detector = value.get("detector")
    media = value.get("media_format")
    if not isinstance(detector, dict) or not isinstance(media, dict):
        raise ValueError("benchmark profile is missing detector/media_format")
    sample_rate = int(detector.get("sample_rate", 0))
    frame_ms = int(detector.get("window_ms", 0))
    if sample_rate != settings.sample_rate:
        raise ValueError("runtime sample_rate does not match frozen profile")
    if frame_ms != settings.frame_ms:
        raise ValueError("runtime frame_ms does not match frozen profile")
    if int(media.get("analysis_sample_rate", 0)) != settings.sample_rate:
        raise ValueError("analysis sample rate does not match runtime")
    if int(media.get("frame_ms", 0)) != settings.frame_ms:
        raise ValueError("analysis frame duration does not match runtime")
    return FrozenRuntimeProfile(
        path=profile_path,
        sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        activity_threshold_dbfs=float(detector["activity_threshold_dbfs"]),
        sample_rate=sample_rate,
        frame_ms=frame_ms,
    )


def build_milestone_sink(
    settings: Settings,
) -> tuple[MilestoneSink, FrozenRuntimeProfile | None]:
    """Construct strict-mode instrumentation or fail before accepting calls."""
    if settings.benchmark_mode not in STRICT_MODES:
        return NullMilestoneSink(), None
    missing = [
        name
        for name, value in (
            ("benchmark_trial_id", settings.benchmark_trial_id),
            ("benchmark_events_path", settings.benchmark_events_path),
            ("benchmark_profile_path", settings.benchmark_profile_path),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"strict benchmark mode requires {', '.join(missing)}")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", settings.benchmark_trial_id) is None:
        raise ValueError("benchmark_trial_id contains unsafe characters")
    profile = load_frozen_runtime_profile(settings.benchmark_profile_path, settings)
    events_path = Path(settings.benchmark_events_path)
    if events_path.exists():
        raise ValueError("benchmark events path already exists")
    sink = JsonlMilestoneSink(
        events_path,
        trial_id=settings.benchmark_trial_id,
        condition=settings.benchmark_mode,
        profile_sha256=profile.sha256,
    )
    matched_config = matched_runtime_config(settings)
    sink.record(
        "benchmark_runtime_ready",
        trigger_policy=settings.benchmark_mode.value,
        half_duplex=settings.half_duplex,
        speech_bearing_rule=SPEECH_BEARING_RULE_VERSION,
        activity_threshold_dbfs=profile.activity_threshold_dbfs,
        sample_rate=profile.sample_rate,
        frame_ms=profile.frame_ms,
        matched_config=matched_config,
        matched_config_sha256=hashlib.sha256(
            json.dumps(
                matched_config, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    )
    return sink, profile


def pcm16_rms_dbfs(pcm16: bytes) -> float:
    """Return RMS dBFS for one little-endian PCM16 frame."""
    if not pcm16 or len(pcm16) % 2:
        return -math.inf
    samples = array("h")
    samples.frombytes(pcm16)
    total = sum(int(sample) * int(sample) for sample in samples)
    rms = math.sqrt(total / len(samples))
    return -math.inf if rms == 0 else 20.0 * math.log10(rms / 32_768.0)


def interruption_source(
    mode: BenchmarkMode, event_type: str, transcript: str
) -> str | None:
    """Return the sole eligible source under a strict policy, if any."""
    if mode == BenchmarkMode.ONSET_FD_VAD:
        return "vad" if event_type == "speech_started" else None
    if mode == BenchmarkMode.ONSET_FD_TRANSCRIPT:
        if (
            event_type in {"transcript_interim", "transcript_final"}
            and transcript.strip()
        ):
            return event_type
        return None
    if mode == BenchmarkMode.ONSET_HALF_DUPLEX:
        return None
    if event_type == "speech_started":
        return "vad"
    if event_type in {"transcript_interim", "transcript_final"} and transcript.strip():
        return event_type
    return None
