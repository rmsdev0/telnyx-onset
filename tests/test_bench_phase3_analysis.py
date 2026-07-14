from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from bench.phase3_analysis import _percentile, analyze_session


def _trial(condition: str, run_id: str, latency: float) -> dict[str, object]:
    return {
        "condition": condition,
        "harness_run_id": run_id,
        "classification": {"eligible": True, "successful": True, "failure_codes": []},
        "metrics": {"harness_boundary_latency_ms": latency},
    }


def _write_events(path: Path, condition: str) -> None:
    names = (
        ("vad_decision", {"eligible_for_interruption": True})
        if condition == "onset-fd-vad"
        else (
            "first_transcript_interim",
            {"eligible_for_interruption": True, "non_empty": True},
        )
    )
    rows = [
        {"event": names[0], "clock_id": "c", "monotonic_ns": 1, **names[1]},
        {"event": "interruption_requested", "clock_id": "c", "monotonic_ns": 2},
        {
            "event": "local_media_epoch_invalidated",
            "clock_id": "c",
            "monotonic_ns": 3,
        },
        {"event": "clear_send_started", "clock_id": "c", "monotonic_ns": 4},
        {
            "event": "clear_send_finished",
            "clock_id": "c",
            "monotonic_ns": 5,
            "outcome": "completed",
        },
    ]
    path.mkdir()
    (path / "agent_events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )


def test_percentile_uses_linear_interpolation() -> None:
    assert _percentile([0.0, 10.0], 0.25) == pytest.approx(2.5)


def test_analysis_is_unpaired_and_counts_attempts(tmp_path: Path) -> None:
    _write_events(tmp_path / "vad", "onset-fd-vad")
    _write_events(tmp_path / "transcript", "onset-fd-transcript")
    session = {
        "manifest": "bench/phase3_final_manifest.json",
        "trials": [
            _trial("onset-fd-vad", "vad", 100.0),
            _trial("onset-fd-transcript", "transcript", 300.0),
        ],
    }
    result = analyze_session(
        session, artifact_root=tmp_path, seed=7, bootstrap_resamples=20
    )
    assert result["comparison"] == {
        "design": "unpaired independent trials",
        "direction": "transcript_minus_vad_ms",
        "median_difference_ms": 200.0,
    }
    assert result["qualification_excluded"] is False
