from __future__ import annotations

import json
import wave
from array import array
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bench.calibration_review import (
    build_calibration_evidence,
    load_segment_specs,
)


def _write_run(root: Path, run_id: str, samples: array[int]) -> None:
    run = root / run_id
    run.mkdir()
    with wave.open(str(run / "rx_8k.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(samples.tobytes())
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "capture_mode": "agent-only",
                "gate_outcome": "CONTROL_CAPTURE_COMPLETE_PENDING_REVIEW",
                "git_commit": "a" * 40,
            }
        )
    )


def test_calibration_evidence_records_grid_without_selection(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    loud = array("h", [8_000] * 1_600)
    short_pause = array("h", [0] * 800)
    trailing_silence = array("h", [0] * 8_000)
    _write_run(artifact_root, "p2-aaaaaaaaaaaaaaaa", loud + short_pause + loud)
    _write_run(artifact_root, "p2-bbbbbbbbbbbbbbbb", loud + trailing_silence)
    _write_run(artifact_root, "p2-cccccccccccccccc", trailing_silence)
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "run_id": "p2-aaaaaaaaaaaaaaaa",
                        "label": "pause",
                        "kind": "natural_pause",
                        "start_sample_8k": 0,
                        "end_sample_8k": 4_000,
                    },
                    {
                        "run_id": "p2-bbbbbbbbbbbbbbbb",
                        "label": "stop",
                        "kind": "forced_stop",
                        "start_sample_8k": 0,
                        "end_sample_8k": 9_600,
                    },
                    {
                        "run_id": "p2-cccccccccccccccc",
                        "label": "silence",
                        "kind": "silence",
                        "start_sample_8k": 0,
                        "end_sample_8k": 8_000,
                    },
                ]
            }
        )
    )
    specs = load_segment_specs(labels)
    evidence = build_calibration_evidence(artifact_root, specs)
    assert evidence["selected_profile"] is None
    assert evidence["passing_candidates"]
    records = evidence["records"]
    assert isinstance(records, list)
    assert len(records) == 4 * 4 * 10 * 3


def test_labels_require_all_kinds_and_safe_run_id(tmp_path: Path) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "run_id": "../escape",
                        "label": "bad",
                        "kind": "natural_pause",
                        "start_sample_8k": 0,
                        "end_sample_8k": 1,
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="calibration_segment_invalid"):
        load_segment_specs(labels)
