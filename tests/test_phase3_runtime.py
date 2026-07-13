"""Adversarial qualification of strict Phase 3 runtime behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from onset.benchmark import build_milestone_sink
from onset.settings import Settings
from onset.types import BenchmarkMode
from tests.conftest import make_harness, until


class RecordingSink:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(
        self, event: str, *, monotonic_ns: int | None = None, **fields: object
    ) -> None:
        self.rows.append(
            {"event": event, "monotonic_ns": monotonic_ns, **fields}
        )

    def events(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["event"] == name]


def strict_settings(mode: BenchmarkMode) -> Settings:
    return Settings(
        telnyx_api_key="k",
        telnyx_public_key="p",
        half_duplex=False,
        benchmark_mode=mode,
    )


@pytest.mark.asyncio
async def test_vad_mode_transcript_is_inert_but_turn_is_preserved() -> None:
    sink = RecordingSink()
    h = make_harness(
        strict_settings(BenchmarkMode.ONSET_FD_VAD), auto_complete=False
    )
    h.agent._milestones = sink
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)

    h.agent.submit_transcript("I need", is_final=False)
    await asyncio.sleep(0.03)
    assert h.media.clears == 0
    assert h.agent._barge_in.agent_is_speaking
    assert sink.events("ineligible_trigger_observed")

    h.vad.fire = True
    h.agent.handle_audio(b"\x00" * 640)
    await until(lambda: h.media.clears == 1)
    h.agent.submit_transcript("I need a table for two", True, speech_final=True)
    await until(lambda: len(sink.events("caller_turn_completed")) == 1)

    interruptions = sink.events("interruption_requested")
    assert [row["source"] for row in interruptions] == ["vad"]
    assert len(sink.events("response_cancellation_requested")) == 1
    assert len(sink.events("local_media_epoch_invalidated")) == 1
    assert len(sink.events("clear_send_started")) == 1
    assert sink.events("clear_send_finished")[0]["outcome"] == "completed"
    user_turns = [
        row
        for row in h.agent._conversation.to_log_dict()
        if row["role"] == "user"
    ]
    assert len(user_turns) == 1
    assert user_turns[0]["content"] == "I need a table for two"

    h.agent.submit_hangup()
    assert h.agent.run_task is not None
    await asyncio.wait_for(h.agent.run_task, timeout=2)


@pytest.mark.asyncio
async def test_transcript_mode_vad_is_inert_and_first_transcript_interrupts() -> None:
    sink = RecordingSink()
    h = make_harness(
        strict_settings(BenchmarkMode.ONSET_FD_TRANSCRIPT), auto_complete=False
    )
    h.agent._milestones = sink
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)

    h.vad.fire = True
    h.agent.handle_audio(b"\x00" * 640)
    await asyncio.sleep(0.03)
    assert h.media.clears == 0
    assert h.agent._barge_in.agent_is_speaking

    h.agent.submit_transcript("actually", is_final=False)
    await until(lambda: h.media.clears == 1)
    h.agent.submit_transcript("actually, a table for two", True, speech_final=True)
    await until(lambda: len(sink.events("caller_turn_completed")) == 1)

    interruptions = sink.events("interruption_requested")
    assert [row["source"] for row in interruptions] == ["transcript_interim"]
    ineligible = sink.events("ineligible_trigger_observed")
    assert [row["source"] for row in ineligible] == ["speech_started"]
    completed = sink.events("caller_turn_completed")[0]
    assert completed["source_event_count"] == 3  # interim, final, endpoint
    next_responses = sink.events("next_response_started")
    assert next_responses[-1]["caller_turn_id"] == completed["turn_id"]

    h.agent.submit_hangup()
    assert h.agent.run_task is not None
    await asyncio.wait_for(h.agent.run_task, timeout=2)


def test_strict_mode_rejects_half_duplex_mismatch() -> None:
    with pytest.raises(ValidationError, match="requires half_duplex=false"):
        Settings(
            telnyx_api_key="k",
            telnyx_public_key="p",
            half_duplex=True,
            benchmark_mode=BenchmarkMode.ONSET_FD_VAD,
        )
    with pytest.raises(ValidationError, match="requires half_duplex=true"):
        Settings(
            telnyx_api_key="k",
            telnyx_public_key="p",
            half_duplex=False,
            benchmark_mode=BenchmarkMode.ONSET_HALF_DUPLEX,
        )


def test_strict_sink_requires_frozen_profile_and_write_once_path(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    events = tmp_path / "events.jsonl"
    settings = Settings(
        telnyx_api_key="k",
        telnyx_public_key="p",
        half_duplex=False,
        benchmark_mode=BenchmarkMode.ONSET_FD_VAD,
        benchmark_trial_id="p3q-001",
        benchmark_events_path=str(events),
        benchmark_profile_path=str(root / "bench" / "measurement_profile.json"),
    )
    _, profile = build_milestone_sink(settings)
    assert profile is not None
    row = json.loads(events.read_text().splitlines()[0])
    assert row["event"] == "benchmark_runtime_ready"
    assert row["condition"] == "onset-fd-vad"
    assert row["trial_id"] == "p3q-001"
    assert row["matched_config_sha256"]
    assert "telnyx_api_key" not in row["matched_config"]
    with pytest.raises(ValueError, match="already exists"):
        build_milestone_sink(settings)


def test_strict_sink_rejects_profile_runtime_mismatch(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = Settings(
        telnyx_api_key="k",
        telnyx_public_key="p",
        half_duplex=False,
        sample_rate=8_000,
        benchmark_mode=BenchmarkMode.ONSET_FD_TRANSCRIPT,
        benchmark_trial_id="p3q-002",
        benchmark_events_path=str(tmp_path / "events.jsonl"),
        benchmark_profile_path=str(root / "bench" / "measurement_profile.json"),
    )
    with pytest.raises(ValueError, match="sample_rate"):
        build_milestone_sink(settings)
