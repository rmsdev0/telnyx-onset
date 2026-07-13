"""Offline tests for Phase 3 live evidence interpretation."""

from __future__ import annotations

from bench.live_phase3 import _agent_evidence
from onset.types import BenchmarkMode


def _row(event: str, timestamp: int, **fields: object) -> dict[str, object]:
    return {"event": event, "monotonic_ns": timestamp, **fields}


def test_transcript_final_is_an_eligible_transcript_trigger() -> None:
    rows = [
        _row(
            "benchmark_runtime_ready",
            1,
            condition="onset-fd-transcript",
            profile_sha256="p",
            matched_config_sha256="c",
        ),
        _row("vad_decision", 2),
        _row("ineligible_trigger_observed", 3, source="speech_started"),
        _row("interruption_requested", 4, source="transcript_final"),
        _row("local_media_epoch_invalidated", 5),
        _row("clear_send_finished", 6, outcome="completed"),
        _row("caller_turn_completed", 7, turn_id=1),
        _row("next_response_started", 8, caller_turn_id=1),
    ]
    evidence, config_hash = _agent_evidence(
        rows, BenchmarkMode.ONSET_FD_TRANSCRIPT
    )
    assert evidence["eligible_trigger_count"] == 1
    assert evidence["interrupt_action_count"] == 1
    assert evidence["caller_turn_count"] == 1
    assert evidence["next_response_based_on_turn"] is True
    assert evidence["event_order_valid"] is True
    assert config_hash == "c"


def test_wrong_source_does_not_become_eligible_and_bad_order_is_visible() -> None:
    rows = [
        _row("benchmark_runtime_ready", 2, matched_config_sha256="c"),
        _row("interruption_requested", 1, source="transcript_interim"),
    ]
    evidence, _ = _agent_evidence(rows, BenchmarkMode.ONSET_FD_VAD)
    assert evidence["eligible_trigger_count"] == 0
    assert evidence["event_order_valid"] is False
