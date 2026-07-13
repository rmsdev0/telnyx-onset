"""Build sanitized, non-selecting detector-calibration evidence.

The input labels are human-reviewed sample ranges from ignored local capture
artifacts. This module evaluates the preregistered finite candidate grid and
records every per-segment result; it never chooses or freezes a profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from bench.acoustic_stop import (
    CALIBRATION_ACTIVITY_THRESHOLDS_DBFS,
    CALIBRATION_HOLD_STEP_MS,
    CALIBRATION_SILENCE_THRESHOLDS_DBFS,
    CALIBRATION_WINDOW_SIZES_MS,
    LabeledCalibrationSegment,
    evaluate_bounded_calibration,
)


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    run_id: str
    label: str
    kind: Literal["natural_pause", "forced_stop"]
    start_sample_8k: int
    end_sample_8k: int


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("labels_not_object")
    return value


def load_segment_specs(path: Path) -> tuple[SegmentSpec, ...]:
    root = _read_object(path)
    raw_segments = root.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("calibration_segments_required")
    specs: list[SegmentSpec] = []
    labels: set[str] = set()
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("calibration_segment_invalid")
        run_id = raw.get("run_id")
        label = raw.get("label")
        kind = raw.get("kind")
        start = raw.get("start_sample_8k")
        end = raw.get("end_sample_8k")
        if (
            not isinstance(run_id, str)
            or not run_id.startswith("p2-")
            or "/" in run_id
            or not isinstance(label, str)
            or not label
            or label in labels
            or kind not in {"natural_pause", "forced_stop"}
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            raise ValueError("calibration_segment_invalid")
        labels.add(label)
        specs.append(
            SegmentSpec(
                run_id=run_id,
                label=label,
                kind=kind,
                start_sample_8k=start,
                end_sample_8k=end,
            )
        )
    if {spec.kind for spec in specs} != {"natural_pause", "forced_stop"}:
        raise ValueError("both_calibration_label_kinds_required")
    return tuple(specs)


def _read_pcm8k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != 8_000
            or wav.getcomptype() != "NONE"
        ):
            raise ValueError("calibration_wav_format_mismatch")
        return wav.readframes(wav.getnframes())


def _zoh_8k_to_16k(pcm8k: bytes) -> bytes:
    source = array("h")
    source.frombytes(pcm8k)
    output = array("h", bytes(len(source) * 4))
    output[0::2] = source
    output[1::2] = source
    return output.tobytes()


def build_calibration_evidence(
    artifact_root: Path, specs: tuple[SegmentSpec, ...]
) -> dict[str, object]:
    segments: list[LabeledCalibrationSegment] = []
    sources: dict[str, dict[str, object]] = {}
    for spec in specs:
        run_dir = (artifact_root / spec.run_id).resolve()
        if run_dir.parent != artifact_root.resolve():
            raise ValueError("calibration_artifact_escape")
        wav_path = run_dir / "rx_8k.wav"
        manifest_path = run_dir / "manifest.json"
        pcm8k = _read_pcm8k(wav_path)
        samples = len(pcm8k) // 2
        if spec.end_sample_8k > samples:
            raise ValueError("calibration_segment_out_of_bounds")
        selected = pcm8k[spec.start_sample_8k * 2 : spec.end_sample_8k * 2]
        segments.append(
            LabeledCalibrationSegment(
                label=spec.label,
                kind=spec.kind,
                pcm16=_zoh_8k_to_16k(selected),
            )
        )
        if spec.run_id not in sources:
            manifest = _read_object(manifest_path)
            sources[spec.run_id] = {
                "capture_mode": manifest.get("capture_mode"),
                "gate_outcome": manifest.get("gate_outcome"),
                "git_commit": manifest.get("git_commit"),
                "rx_wav_sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
            }

    records = evaluate_bounded_calibration(
        tuple(segments),
        activity_thresholds_dbfs=CALIBRATION_ACTIVITY_THRESHOLDS_DBFS,
        silence_thresholds_dbfs=CALIBRATION_SILENCE_THRESHOLDS_DBFS,
        hold_step_ms=CALIBRATION_HOLD_STEP_MS,
        window_sizes_ms=CALIBRATION_WINDOW_SIZES_MS,
    )
    by_candidate: dict[tuple[object, ...], list[bool]] = {}
    for record in records:
        key = (
            record.statistic,
            record.window_ms,
            record.activity_threshold_dbfs,
            record.silence_threshold_dbfs,
            record.sustained_silence_ms,
        )
        by_candidate.setdefault(key, []).append(record.passed)
    passing = [
        {
            "statistic": key[0],
            "window_ms": key[1],
            "activity_threshold_dbfs": key[2],
            "silence_threshold_dbfs": key[3],
            "sustained_silence_ms": key[4],
        }
        for key, results in by_candidate.items()
        if all(results) and len(results) == len(segments)
    ]
    return {
        "schema_version": 1,
        "selected_profile": None,
        "candidate_grid": {
            "statistics": ["rms_dbfs"],
            "window_sizes_ms": list(CALIBRATION_WINDOW_SIZES_MS),
            "activity_thresholds_dbfs": list(
                CALIBRATION_ACTIVITY_THRESHOLDS_DBFS
            ),
            "silence_thresholds_dbfs": list(
                CALIBRATION_SILENCE_THRESHOLDS_DBFS
            ),
            "hold_step_ms": CALIBRATION_HOLD_STEP_MS,
        },
        "sources": sources,
        "labels": [asdict(spec) for spec in specs],
        "records": [asdict(record) for record in records],
        "passing_candidates": passing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    specs = load_segment_specs(arguments.labels)
    evidence = build_calibration_evidence(arguments.artifact_root, specs)
    arguments.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    records = evidence["records"]
    passing = evidence["passing_candidates"]
    assert isinstance(records, list)
    assert isinstance(passing, list)
    print(
        f"wrote {arguments.output}: {len(records)} records, "
        f"{len(passing)} passing candidates, none selected"
    )


if __name__ == "__main__":
    main()
