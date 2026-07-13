"""Phase 3 attempted-trial ordering and classification tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.phase3 import (
    FailureCode,
    TrialEvidence,
    balanced_condition_order,
    build_manifest,
    classify_trial,
    write_new_json,
)
from onset.types import BenchmarkMode


def successful_evidence(**changes: object) -> TrialEvidence:
    values: dict[str, object] = {
        "agent_audio_active": True,
        "stimulus_started": True,
        "stimulus_delivered": True,
        "media_capture_present": True,
        "configuration_matches": True,
        "event_order_valid": True,
        "instrumentation_complete": True,
        "transport_complete": True,
        "eligible_trigger_count": 1,
        "interrupt_action_count": 1,
        "acoustic_stop_found": True,
        "stop_before_natural_end": True,
        "caller_turn_count": 1,
        "stale_audio_resumed": False,
        "next_response_based_on_turn": True,
    }
    values.update(changes)
    return TrialEvidence(**values)  # type: ignore[arg-type]


def test_balanced_order_is_deterministic_and_block_balanced() -> None:
    a = balanced_condition_order(seed=8675309, attempts_per_condition=10)
    b = balanced_condition_order(seed=8675309, attempts_per_condition=10)
    assert a == b
    assert a.count(BenchmarkMode.ONSET_FD_VAD) == 10
    assert a.count(BenchmarkMode.ONSET_FD_TRANSCRIPT) == 10
    for start in range(0, len(a), 4):
        block = a[start : start + 4]
        assert block.count(BenchmarkMode.ONSET_FD_VAD) == 2
        assert block.count(BenchmarkMode.ONSET_FD_TRANSCRIPT) == 2


def test_classifier_keeps_overlapping_failures_and_eligibility_separate() -> None:
    result = classify_trial(
        successful_evidence(
            stimulus_delivered=False,
            media_capture_present=False,
            eligible_trigger_count=0,
            interrupt_action_count=0,
            caller_turn_count=0,
        )
    )
    assert not result.eligible
    assert not result.successful
    assert FailureCode.STIMULUS_DELIVERY_FAILED in result.failure_codes
    assert FailureCode.MEDIA_CAPTURE_MISSING in result.failure_codes
    assert FailureCode.TRIGGER_NOT_OBSERVED in result.failure_codes
    assert FailureCode.CALLER_TURN_LOST in result.failure_codes


def test_success_requires_exactly_once_trigger_action_and_turn() -> None:
    assert classify_trial(successful_evidence()).successful
    duplicate = classify_trial(
        successful_evidence(interrupt_action_count=2, caller_turn_count=2)
    )
    assert duplicate.eligible
    assert not duplicate.successful
    assert FailureCode.DUPLICATE_INTERRUPT_ACTION in duplicate.failure_codes
    assert FailureCode.CALLER_TURN_DUPLICATED in duplicate.failure_codes


def test_formal_manifest_records_commit_profile_and_attempts() -> None:
    root = Path(__file__).parents[1]
    profile = root / "bench" / "measurement_profile.json"
    manifest = build_manifest(
        stage="qualification",
        seed=17,
        attempts_per_condition=10,
        profile_path=profile,
        repo_root=root,
        require_clean=False,
    )
    assert manifest["stage"] == "qualification"
    assert manifest["matched_difference"] == "trigger eligibility only"
    assert len(manifest["trials"]) == 20  # type: ignore[arg-type]
    assert manifest["git"]["commit"]  # type: ignore[index]
    assert manifest["measurement_profile"]["sha256"]  # type: ignore[index]
    protocol = manifest["qualification_protocol"]
    assert protocol["barge_offset_ms"] == 1_000  # type: ignore[index]
    assert protocol["natural_end_reference_ms"] == 3_200  # type: ignore[index]


def test_evidence_file_is_write_once(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    write_new_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    with pytest.raises(FileExistsError):
        write_new_json(path, {"a": 2})
