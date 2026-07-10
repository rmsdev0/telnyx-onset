"""Fixture, pacing, and isolated controller tests; all network-free."""

from __future__ import annotations

import asyncio
import json
import wave
from array import array
from typing import TYPE_CHECKING, Any, cast

import pytest

from bench.acoustic_probe import (
    BenchConfig,
    Fixture,
    ProbeController,
    ProbeState,
    SafeCallControl,
    analyze_contamination,
    load_fixture,
    send_fixture_paced,
)
from onset.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


def write_wav(
    path: Path,
    samples: array[int],
    *,
    rate: int = 16_000,
    channels: int = 1,
    width: int = 2,
) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(samples.tobytes())


def test_fixture_validation_hash_onset_and_final_padding(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.wav"
    samples = array("h", [0] * 10 + [1_000] * 315)
    write_wav(path, samples)
    fixture = load_fixture(path)
    assert fixture.first_active_sample == 10
    assert fixture.onset_method == "first_pcm16_sample_abs_gte_threshold"
    assert len(fixture.sha256) == 64
    assert len(fixture.frames) == 2
    assert len(fixture.frames[-1]) == 640
    assert fixture.frames[-1].endswith(b"\x00" * 630)


@pytest.mark.parametrize(
    ("rate", "channels", "width", "error"),
    [
        (8_000, 1, 2, "fixture_wrong_sample_rate"),
        (16_000, 2, 2, "fixture_wrong_channels"),
        (16_000, 1, 1, "fixture_wrong_sample_width"),
    ],
)
def test_fixture_rejects_wrong_format(
    tmp_path: Path, rate: int, channels: int, width: int, error: str
) -> None:
    path = tmp_path / "wrong.wav"
    write_wav(
        path, array("h", [1_000] * 320), rate=rate, channels=channels, width=width
    )
    with pytest.raises(ValueError, match=error):
        load_fixture(path)


def test_fixture_rejects_silence_size_and_duration(tmp_path: Path) -> None:
    silence = tmp_path / "silence.wav"
    write_wav(silence, array("h", [0] * 320))
    with pytest.raises(ValueError, match="fixture_all_silence"):
        load_fixture(silence)

    active = tmp_path / "active.wav"
    write_wav(active, array("h", [1_000] * 321))
    with pytest.raises(ValueError, match="fixture_too_large"):
        load_fixture(active, maximum_bytes=1)
    with pytest.raises(ValueError, match="fixture_too_long"):
        load_fixture(active, maximum_seconds=0.001)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000_000

    def monotonic_ns(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += int(seconds * 1_000_000_000)


class FakeSender:
    def __init__(self, clock: FakeClock, *, cancel_after: int | None = None) -> None:
        self.clock = clock
        self.messages: list[dict[str, Any]] = []
        self.cancel_after = cancel_after

    async def send_text(self, data: str) -> None:
        if self.cancel_after is not None and len(self.messages) >= self.cancel_after:
            raise asyncio.CancelledError
        self.messages.append(json.loads(data))
        self.clock.now += 1_000_000


@pytest.mark.asyncio
async def test_absolute_deadline_pacing_and_lateness_accounting() -> None:
    fixture = Fixture(
        pcm16=b"\x01\x00" * 640,
        sha256="0" * 64,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        frame_bytes=640,
        first_active_sample=0,
        onset_method="test",
        onset_threshold=1,
    )
    clock = FakeClock()
    sender = FakeSender(clock)
    summary = await send_fixture_paced(sender, fixture, clock=clock, sleep=clock.sleep)
    assert len(sender.messages) == 2
    assert [record.intended_deadline_ns for record in summary.frames] == [
        1_000_000_000,
        1_020_000_000,
    ]
    assert summary.maximum_send_duration_ns == 1_000_000
    assert summary.within_tolerance


@pytest.mark.asyncio
async def test_cancellation_during_paced_send_propagates() -> None:
    fixture = Fixture(
        pcm16=b"\x01\x00" * 640,
        sha256="0" * 64,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        frame_bytes=640,
        first_active_sample=0,
        onset_method="test",
        onset_threshold=1,
    )
    clock = FakeClock()
    with pytest.raises(asyncio.CancelledError):
        await send_fixture_paced(
            FakeSender(clock, cancel_after=1), fixture, clock=clock, sleep=clock.sleep
        )


class FakeCallControl:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.actions: list[tuple[str, str, dict[str, object]]] = []
        self.dials = 0

    async def dial(self, *, to: str, from_: str) -> str:
        self.dials += 1
        return "memory-leg-a"

    async def action(
        self,
        call_control_id: str,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.actions.append((call_control_id, action, payload or {}))

    async def aclose(self) -> None:
        pass


def config(tmp_path: Path) -> BenchConfig:
    settings = Settings(
        telnyx_api_key="local-test-key",
        telnyx_public_key="local-test-public-key",
        telnyx_connection_id="local-connection",
    )
    fixture = Fixture(
        pcm16=b"\x01\x00" * 320,
        sha256="f" * 64,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        frame_bytes=640,
        first_active_sample=0,
        onset_method="test",
        onset_threshold=1,
    )
    return BenchConfig(
        settings=settings,
        agent_number="agent-number-memory-only",
        harness_number="harness-number-memory-only",
        public_wss_base="wss://example.invalid",
        target_legs="opposite",
        fixture=fixture,
        artifacts_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_controller_owns_leg_a_routes_leg_b_and_bridges(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    controller = ProbeController(cfg, cast("SafeCallControl", fake))
    await controller.dial()
    assert controller.leg_a == "memory-leg-a"
    await controller.dispatch(
        "call.initiated",
        {
            "call_control_id": "memory-leg-b",
            "direction": "incoming",
            "from": cfg.harness_number,
            "to": cfg.agent_number,
        },
    )
    await controller.dispatch("call.answered", {"call_control_id": "memory-leg-a"})
    await controller.dispatch("call.answered", {"call_control_id": "memory-leg-b"})
    assert ProbeState.LEG_A_IDENTIFIED in controller.states
    assert ProbeState.LEG_B_IDENTIFIED in controller.states
    assert ProbeState.LEGS_ANSWERED in controller.states
    streams = [item for item in fake.actions if item[1] == "streaming_start"]
    assert len(streams) == 2
    probe_payload = next(
        payload for ccid, _, payload in streams if ccid == "memory-leg-a"
    )
    assert probe_payload["stream_track"] == "both_tracks"
    assert probe_payload["stream_bidirectional_target_legs"] == "opposite"
    assert "token=" not in str(probe_payload["stream_url"])
    assert "stream_auth_token" in probe_payload
    bridges = [item for item in fake.actions if item[1] == "bridge"]
    assert len(bridges) == 1
    manifest = (controller.artifacts.path / "manifest.json").read_text()
    assert cfg.agent_number not in manifest
    assert cfg.harness_number not in manifest
    assert "memory-leg-a" not in manifest
    assert "memory-leg-b" not in manifest


@pytest.mark.asyncio
async def test_controller_teardown_attempts_both_legs(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    controller = ProbeController(cfg, cast("SafeCallControl", fake))
    controller.leg_a = "memory-leg-a"
    controller.leg_b = "memory-leg-b"
    await controller.hangup_both()
    assert {(ccid, action) for ccid, action, _ in fake.actions} == {
        ("memory-leg-a", "hangup"),
        ("memory-leg-b", "hangup"),
    }


@pytest.mark.asyncio
async def test_controller_rejects_unrelated_inbound_leg(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    controller = ProbeController(cfg, cast("SafeCallControl", fake))
    await controller.dial()
    await controller.dispatch(
        "call.initiated",
        {
            "call_control_id": "unrelated",
            "direction": "incoming",
            "from": "unexpected",
            "to": cfg.agent_number,
        },
    )
    assert controller.leg_b is None
    assert not fake.actions


def test_live_config_requires_explicit_target_and_hard_bounds(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    with pytest.raises(ValueError, match="target_legs"):
        BenchConfig(
            settings=cfg.settings,
            agent_number=cfg.agent_number,
            harness_number=cfg.harness_number,
            public_wss_base=cfg.public_wss_base,
            target_legs="both",
            fixture=cfg.fixture,
            artifacts_root=tmp_path / "two",
        )
    with pytest.raises(ValueError, match="call duration"):
        BenchConfig(
            settings=cfg.settings,
            agent_number=cfg.agent_number,
            harness_number=cfg.harness_number,
            public_wss_base=cfg.public_wss_base,
            target_legs="self",
            fixture=cfg.fixture,
            call_seconds=61,
            artifacts_root=tmp_path / "three",
        )
    with pytest.raises(ValueError, match="attempt"):
        BenchConfig(
            settings=cfg.settings,
            agent_number=cfg.agent_number,
            harness_number=cfg.harness_number,
            public_wss_base=cfg.public_wss_base,
            target_legs="self",
            fixture=cfg.fixture,
            attempts=4,
            artifacts_root=tmp_path / "four",
        )
    unsafe = Settings(
        telnyx_api_key="local-test-key",
        telnyx_public_key="local-test-public-key",
        half_duplex=False,
    )
    with pytest.raises(ValueError, match="half-duplex"):
        BenchConfig(
            settings=unsafe,
            agent_number=cfg.agent_number,
            harness_number=cfg.harness_number,
            public_wss_base=cfg.public_wss_base,
            target_legs="self",
            fixture=cfg.fixture,
            artifacts_root=tmp_path / "five",
        )


def test_contamination_metric_rejects_mixed_tracks() -> None:
    stimulus = array("h", [0, 1_000, -1_000, 2_000] * 100).tobytes()
    quiet_agent = array("h", [0] * 400).tobytes()
    separated = analyze_contamination(quiet_agent, stimulus)
    mixed = analyze_contamination(stimulus, stimulus)
    assert separated.passed
    assert separated.agent_to_stimulus_energy_ratio < 0.001
    assert not mixed.passed
    assert mixed.absolute_waveform_correlation == pytest.approx(1.0)
