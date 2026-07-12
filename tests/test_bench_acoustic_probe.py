"""Fixture, pacing, and isolated controller tests; all network-free."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import wave
from array import array
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bench.acoustic_probe as acoustic_probe
from bench.acoustic_probe import (
    BenchConfig,
    Fixture,
    GreetingObservedMedia,
    PacingSummary,
    ProbeController,
    ProbeState,
    SafeCallControl,
    analyze_contamination,
    create_app,
    load_fixture,
    match_fixture_reference,
    send_fixture_paced,
)
from bench.acoustic_stop import DetectorConfig, analyze_acoustic_stop
from bench.media_capture import (
    TOKEN_TTL_NS,
    BoundedCapture,
    CrossLegCapture,
    MediaFrame,
    ProbeProtocolError,
)
from onset.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

    from onset.media import MediaStream


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


def test_oversized_fixture_is_rejected_from_stat_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "oversized.wav"
    path.write_bytes(b"x" * 128)

    def should_not_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("oversized fixture payload must not be read")

    monkeypatch.setattr(type(path), "open", should_not_open)
    with pytest.raises(ValueError, match="fixture_too_large"):
        load_fixture(path, maximum_bytes=64)


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

    async def dial(
        self, *, to: str, from_: str, connection_id: str, webhook_url: str
    ) -> str:
        self.dials += 1
        assert connection_id == "local-harness-connection"
        assert webhook_url == "https://example.invalid/webhook"
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

    def call(self, call_control_id: str) -> object:
        return object()


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
        harness_connection_id="local-harness-connection",
        public_wss_base="wss://example.invalid",
        target_legs="opposite",
        fixture=fixture,
        artifacts_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_watchdog_greeting_deadline_starts_at_delayed_measurement_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    clock = FakeClock()
    controller = ProbeController(cfg, cast("SafeCallControl", fake), clock=clock)
    controller.leg_a = "memory-leg-a"
    controller.leg_b = "memory-leg-b"
    controller.states.extend(
        (
            ProbeState.LEGS_ANSWERED,
            ProbeState.BRIDGED,
            ProbeState.STREAM_CONNECTED,
            ProbeState.MEDIA_FORMAT_VALIDATED,
        )
    )
    measurement_start: int | None = None

    async def advance(_: float) -> None:
        nonlocal measurement_start
        clock.now += 1_000_000_000
        if measurement_start is None and clock.now == 6_000_000_000:
            controller.mark_media_ready(acoustic_probe.PROBE_CHANNEL, clock.now)
            controller.mark_media_ready(acoustic_probe.AGENT_CHANNEL, clock.now)
            measurement_start = clock.now
        if measurement_start is not None and clock.now < measurement_start + (
            acoustic_probe.MAX_GREETING_ANALYSIS_SECONDS * 1_000_000_000
        ):
            assert controller.failure is None

    monkeypatch.setattr(asyncio, "sleep", advance)
    await controller.watchdog()
    assert measurement_start == 6_000_000_000
    assert clock.now == measurement_start + (
        acoustic_probe.MAX_GREETING_ANALYSIS_SECONDS * 1_000_000_000
    )
    assert controller.failure == "agent_audio_not_observed"


@pytest.mark.asyncio
async def test_watchdog_resets_full_horizon_at_first_greeting_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    clock = FakeClock()
    controller = ProbeController(cfg, cast("SafeCallControl", fake), clock=clock)
    controller.leg_a = "memory-leg-a"
    controller.leg_b = "memory-leg-b"
    controller.states.extend(
        (
            ProbeState.LEGS_ANSWERED,
            ProbeState.BRIDGED,
            ProbeState.STREAM_CONNECTED,
            ProbeState.MEDIA_FORMAT_VALIDATED,
        )
    )
    controller.mark_media_ready(acoustic_probe.PROBE_CHANNEL, clock.now)
    controller.mark_media_ready(acoustic_probe.AGENT_CHANNEL, clock.now)
    greeting_start: int | None = None

    async def advance(_: float) -> None:
        nonlocal greeting_start
        clock.now += 1_000_000_000
        if greeting_start is None and clock.now == 6_000_000_000:
            controller.mark_greeting_started()
            greeting_start = clock.now
        if greeting_start is not None and clock.now < greeting_start + (
            acoustic_probe.MAX_GREETING_ANALYSIS_SECONDS * 1_000_000_000
        ):
            assert controller.failure is None

    monkeypatch.setattr(asyncio, "sleep", advance)
    await controller.watchdog()
    assert greeting_start == 6_000_000_000
    assert clock.now == greeting_start + (
        acoustic_probe.MAX_GREETING_ANALYSIS_SECONDS * 1_000_000_000
    )
    assert controller.failure == "agent_audio_not_observed"


def test_failure_waveforms_require_both_validated_channels_and_are_bounded(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    controller = ProbeController(
        cfg, cast("SafeCallControl", FakeCallControl(cfg.settings))
    )
    capture = controller.capture_after_authentication()
    pcm = array("h", [1_000] * 320).tobytes()
    for sequence, channel in enumerate(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL), start=2
    ):
        capture.append(
            channel,
            MediaFrame(sequence, channel, "inbound", 1, 0, pcm, sequence),
        )
    controller.fail("agent_audio_not_observed")
    controller.write_failure_waveforms()
    assert not list(controller.artifacts.path.glob("diagnostic_*.wav"))

    controller.mark_media_ready(acoustic_probe.PROBE_CHANNEL, 1)
    controller.mark_media_ready(acoustic_probe.AGENT_CHANNEL, 2)
    controller.write_failure_waveforms()
    controller.write_failure_waveforms()
    paths = sorted(controller.artifacts.path.glob("diagnostic_*.wav"))
    assert [path.name for path in paths] == [
        "diagnostic_channel_a.wav",
        "diagnostic_channel_b.wav",
    ]
    for path in paths:
        assert os.stat(path).st_mode & 0o777 == 0o600
        with wave.open(str(path), "rb") as source:
            assert source.getparams()[:4] == (1, 2, 16_000, 320)
            assert source.readframes(320) == pcm


@pytest.mark.asyncio
async def test_greeting_media_adapter_marks_only_first_audio_frame() -> None:
    calls: list[str] = []

    class Inner:
        def begin_utterance(self) -> int:
            return 7

        async def send_audio_frame(self, epoch: int, frame: bytes) -> None:
            calls.append(f"frame:{epoch}:{len(frame)}")

        async def send_mark(self, epoch: int, name: str) -> None:
            calls.append(f"mark:{epoch}:{name}")

        async def flush(self) -> None:
            calls.append("flush")

        async def aclose(self) -> None:
            calls.append("close")

    adapter = GreetingObservedMedia(
        cast("MediaStream", Inner()), lambda: calls.append("first")
    )
    assert adapter.begin_utterance() == 7
    await adapter.send_audio_frame(7, b"a")
    await adapter.send_audio_frame(7, b"bb")
    await adapter.send_mark(7, "done")
    assert calls == ["first", "frame:7:1", "frame:7:2", "mark:7:done"]


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
    assert len(streams) == 1
    assert streams[0][0] == "memory-leg-b"
    await controller.dispatch("call.bridged", {"call_control_id": "memory-leg-a"})
    streams = [item for item in fake.actions if item[1] == "streaming_start"]
    assert len(streams) == 2
    probe_payload = next(
        payload for ccid, _, payload in streams if ccid == "memory-leg-a"
    )
    agent_payload = next(
        payload for ccid, _, payload in streams if ccid == "memory-leg-b"
    )
    assert probe_payload["stream_track"] == "both_tracks"
    assert probe_payload["stream_bidirectional_target_legs"] == "opposite"
    assert agent_payload["stream_track"] == "both_tracks"
    assert agent_payload["stream_bidirectional_target_legs"] == "self"
    assert "token=" not in str(probe_payload["stream_url"])
    assert "stream_auth_token" in probe_payload
    bridges = [item for item in fake.actions if item[1] == "bridge"]
    assert len(bridges) == 1
    manifest = (controller.artifacts.path / "manifest.json").read_text()
    assert cfg.agent_number not in manifest
    assert cfg.harness_number not in manifest
    assert cfg.harness_connection_id not in manifest
    assert cfg.public_webhook_url not in manifest
    assert "memory-leg-a" not in manifest
    assert "memory-leg-b" not in manifest


@pytest.mark.asyncio
async def test_concurrent_bridge_webhooks_start_probe_stream_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    controller = ProbeController(cfg, cast("SafeCallControl", fake))
    controller.leg_a = "memory-leg-a"
    controller.leg_b = "memory-leg-b"
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = 0

    async def delayed_start(ccid: str, *, role: Literal["probe", "agent"]) -> None:
        nonlocal starts
        assert ccid == "memory-leg-a" and role == "probe"
        starts += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(controller, "_start_stream", delayed_start)
    first = asyncio.create_task(
        controller.dispatch("call.bridged", {"call_control_id": "memory-leg-a"})
    )
    await entered.wait()
    second = asyncio.create_task(
        controller.dispatch("call.bridged", {"call_control_id": "memory-leg-b"})
    )
    await asyncio.sleep(0)
    assert starts == 1
    release.set()
    await asyncio.gather(first, second)
    assert controller.streams_started == {"probe"}


@pytest.mark.asyncio
async def test_concurrent_answer_webhooks_send_bridge_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    controller = ProbeController(cfg, cast("SafeCallControl", fake))
    controller.leg_a = "memory-leg-a"
    controller.leg_b = "memory-leg-b"
    agent_started = asyncio.Event()
    release_agent = asyncio.Event()
    bridge_started = asyncio.Event()
    release_bridge = asyncio.Event()
    bridges = 0

    async def delayed_action(
        call_control_id: str,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        nonlocal bridges
        if action == "streaming_start":
            assert call_control_id == "memory-leg-b"
            agent_started.set()
            await release_agent.wait()
        elif action == "bridge":
            assert call_control_id == "memory-leg-a"
            bridges += 1
            bridge_started.set()
            await release_bridge.wait()

    monkeypatch.setattr(fake, "action", delayed_action)
    leg_b = asyncio.create_task(
        controller.dispatch("call.answered", {"call_control_id": "memory-leg-b"})
    )
    await agent_started.wait()
    leg_a = asyncio.create_task(
        controller.dispatch("call.answered", {"call_control_id": "memory-leg-a"})
    )
    await bridge_started.wait()
    release_agent.set()
    await asyncio.sleep(0)
    assert bridges == 1
    release_bridge.set()
    await asyncio.gather(leg_a, leg_b)
    assert controller.bridge_sent


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
async def test_controller_teardown_is_shared_concurrent_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    controller = ProbeController(cfg, cast("SafeCallControl", fake))
    controller.leg_a = "memory-leg-a"
    controller.leg_b = "memory-leg-b"
    both_started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def delayed_action(
        call_control_id: str,
        action: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        assert action == "hangup"
        calls.append(call_control_id)
        if len(calls) == 2:
            both_started.set()
        await release.wait()

    monkeypatch.setattr(fake, "action", delayed_action)
    first = asyncio.create_task(controller.hangup_both())
    second = asyncio.create_task(controller.hangup_both())
    await both_started.wait()
    assert sorted(calls) == ["memory-leg-a", "memory-leg-b"]
    release.set()
    await asyncio.gather(first, second)
    await controller.hangup_both()
    assert sorted(calls) == ["memory-leg-a", "memory-leg-b"]
    assert controller.teardown_result == "both_legs_hung_up"


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
            harness_connection_id=cfg.harness_connection_id,
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
            harness_connection_id=cfg.harness_connection_id,
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
            harness_connection_id=cfg.harness_connection_id,
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
            harness_connection_id=cfg.harness_connection_id,
            public_wss_base=cfg.public_wss_base,
            target_legs="self",
            fixture=cfg.fixture,
            artifacts_root=tmp_path / "five",
        )
    with pytest.raises(ValueError, match="harness connection"):
        BenchConfig(
            settings=cfg.settings,
            agent_number=cfg.agent_number,
            harness_number=cfg.harness_number,
            harness_connection_id="",
            public_wss_base=cfg.public_wss_base,
            target_legs="self",
            fixture=cfg.fixture,
            artifacts_root=tmp_path / "six",
        )
    with pytest.raises(ValueError, match="must be distinct"):
        BenchConfig(
            settings=cfg.settings,
            agent_number=cfg.agent_number,
            harness_number=cfg.harness_number,
            harness_connection_id=cfg.settings.telnyx_connection_id,
            public_wss_base=cfg.public_wss_base,
            target_legs="self",
            fixture=cfg.fixture,
            artifacts_root=tmp_path / "seven",
        )


@pytest.mark.asyncio
async def test_safe_dial_verifies_assignments_and_uses_harness_connection(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            number = request.url.params["filter[phone_number]"]
            connection = (
                cfg.harness_connection_id
                if number == cfg.harness_number
                else cfg.settings.telnyx_connection_id
            )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "phone_number": number,
                            "status": "active",
                            "connection_id": connection,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"data": {"call_control_id": "memory-leg"}})

    control = SafeCallControl(cfg.settings)
    await control._http.aclose()
    control._http = httpx.AsyncClient(
        base_url=cfg.settings.telnyx_api_base,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await control.dial(
            to=cfg.agent_number,
            from_=cfg.harness_number,
            connection_id=cfg.harness_connection_id,
            webhook_url=cfg.public_webhook_url,
        )
    finally:
        await control.aclose()
    assert result == "memory-leg"
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    payload = json.loads(requests[-1].content)
    assert payload == {
        "connection_id": cfg.harness_connection_id,
        "from": cfg.harness_number,
        "to": cfg.agent_number,
        "webhook_url": cfg.public_webhook_url,
        "webhook_url_method": "POST",
    }


@pytest.mark.asyncio
async def test_safe_dial_rejects_assignment_mismatch_before_post(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "phone_number": cfg.harness_number,
                        "status": "active",
                        "connection_id": "wrong",
                    }
                ]
            },
        )

    control = SafeCallControl(cfg.settings)
    await control._http.aclose()
    control._http = httpx.AsyncClient(
        base_url=cfg.settings.telnyx_api_base,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="number_assignment_mismatch"):
            await control.dial(
                to=cfg.agent_number,
                from_=cfg.harness_number,
                connection_id=cfg.harness_connection_id,
                webhook_url=cfg.public_webhook_url,
            )
    finally:
        await control.aclose()
    assert methods == ["GET"]


def test_contamination_metric_rejects_mixed_tracks() -> None:
    stimulus = array("h", [0, 1_000, -1_000, 2_000] * 100).tobytes()
    quiet_agent = array("h", [0] * 400).tobytes()
    separated = analyze_contamination(quiet_agent, stimulus)
    mixed = analyze_contamination(stimulus, stimulus)
    assert separated.passed
    assert separated.agent_to_stimulus_energy_ratio < 0.001
    assert not mixed.passed
    assert mixed.absolute_waveform_correlation == pytest.approx(1.0)


def start_raw(call_id: str, *, encoding: str = "L16") -> str:
    return json.dumps(
        {
            "event": "start",
            "sequence_number": "1",
            "stream_id": "memory-stream",
            "start": {
                "call_control_id": call_id,
                "media_format": {
                    "encoding": encoding,
                    "sample_rate": 16_000,
                    "channels": 1,
                },
            },
        }
    )


def probe_media_raw(
    *, sequence: int, track: str, chunk: int, timestamp: int, pcm16: bytes
) -> str:
    return json.dumps(
        {
            "event": "media",
            "sequence_number": str(sequence),
            "stream_id": "memory-stream",
            "media": {
                "track": track,
                "chunk": str(chunk),
                "timestamp": str(timestamp),
                "payload": base64.b64encode(pcm16).decode(),
            },
        }
    )


def _route_token(
    controller: ProbeController,
    call_id: str = "route-call",
    role: Literal["probe", "agent"] = "probe",
) -> str:
    return controller.tokens.issue(
        controller.run_id, call_id, role, controller.clock.monotonic_ns()
    )


def patterned_fixture() -> tuple[Fixture, tuple[bytes, ...]]:
    frames = tuple(
        array("h", [amplitude] * 320).tobytes()
        for amplitude in (500, 2_000, 8_000, 1_000)
    )
    return (
        Fixture(
            pcm16=b"".join(frames),
            sha256="a" * 64,
            sample_rate=16_000,
            channels=1,
            sample_width=2,
            frame_bytes=640,
            first_active_sample=0,
            onset_method="test",
            onset_threshold=1,
        ),
        frames,
    )


def send_cross_leg_pair(
    ws: Any,
    controller: ProbeController,
    probe_pcm: bytes,
    reference_pcm: bytes,
    state: dict[str, int],
) -> None:
    capture = controller.capture_after_authentication()
    controller.mark_media_ready(acoustic_probe.AGENT_CHANNEL, time.monotonic_ns())
    controller.mark_greeting_started()
    state["agent_chunk"] += 1
    agent_frame = MediaFrame(
        state["agent_sequence"],
        "agent-stream",
        "inbound",
        state["agent_chunk"],
        (state["agent_chunk"] - 1) * 20,
        reference_pcm,
        time.monotonic_ns(),
    )
    if controller.agent_integrity is None:
        controller.agent_integrity = BoundedCapture(
            max_bytes_per_track=1_000_000, max_event_rows=1_000
        )
    controller.agent_integrity.append(agent_frame)
    capture.append(
        acoustic_probe.AGENT_CHANNEL,
        agent_frame,
    )
    state["agent_sequence"] += 1
    state["probe_chunk"] += 1
    ws.send_text(
        probe_media_raw(
            sequence=state["probe_sequence"],
            track="outbound",
            chunk=state["probe_chunk"],
            timestamp=(state["probe_chunk"] - 1) * 20,
            pcm16=probe_pcm,
        )
    )
    state["probe_sequence"] += 1
    expected = state["probe_chunk"]
    for _ in range(1_000):
        if controller.probe_media_frames_received >= expected:
            return
        if (
            controller.failure is not None
            or ProbeState.CAPTURE_COMPLETED in controller.states
        ):
            return
        time.sleep(0.001)
    raise AssertionError("probe route did not consume media frame")


def test_probe_route_stalled_auth_times_out_before_capture_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    allocations = 0
    original = BoundedCapture

    def counted_capture(
        *,
        max_bytes_per_track: int,
        max_event_rows: int,
        tracks: tuple[str, ...] = ("inbound", "outbound"),
    ) -> BoundedCapture:
        nonlocal allocations
        allocations += 1
        return original(
            tracks=tracks,
            max_bytes_per_track=max_bytes_per_track,
            max_event_rows=max_event_rows,
        )

    monkeypatch.setattr(acoustic_probe, "AUTH_HANDSHAKE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(acoustic_probe, "BoundedCapture", counted_capture)
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/probe/{controller.run_id}") as ws,
        ):
            ws.receive_text()
    assert allocations == 0


@pytest.mark.parametrize(
    ("call_id", "encoding", "failure"),
    [
        ("wrong-call", "L16", "stream_call_id_mismatch"),
        ("route-call", "PCMU", "media_format_mismatch"),
    ],
)
def test_probe_route_rejects_start_mismatch(
    tmp_path: Path, call_id: str, encoding: str, failure: str
) -> None:
    cfg = config(tmp_path)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        token = _route_token(controller)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.send_text(start_raw(call_id, encoding=encoding))
            ws.receive_text()
        assert controller.failure == failure


def test_probe_route_requires_bridge_and_hangs_up_both_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    app = create_app(cfg, cast("SafeCallControl", fake))
    monkeypatch.setattr(acoustic_probe, "STATE_TIMEOUT_SECONDS", 0.01)
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.leg_a = "memory-leg-a"
        controller.leg_b = "memory-leg-b"
        token = _route_token(controller)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.send_text(start_raw("route-call"))
            ws.receive_text()
        assert controller.failure == "bridge_failed"
    hangups = {(ccid, action) for ccid, action, _ in fake.actions if action == "hangup"}
    assert hangups == {
        ("memory-leg-a", "hangup"),
        ("memory-leg-b", "hangup"),
    }


def test_probe_route_token_is_one_use_and_only_one_socket_is_active(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        token = _route_token(controller)
        controller.probe_socket_active = True
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.receive_text()
        controller.probe_socket_active = False
        assert controller.tokens.pending == 1


def test_probe_route_rejects_expired_mismatched_and_reused_auth(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        now = controller.clock.monotonic_ns()
        expired = controller.tokens.issue(controller.run_id, "route-call", "probe", now)
        controller.tokens.remove_expired(now + TOKEN_TTL_NS + 1)
        mismatched = controller.tokens.issue(
            "different-run", "route-call", "probe", now
        )
        for token in (expired, mismatched):
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(
                    f"/ws/probe/{controller.run_id}",
                    headers={"x-telnyx-streaming-auth-token": token},
                ) as ws,
            ):
                ws.receive_text()

        token = _route_token(controller)
        controller.bridge_ready.set()
        with client.websocket_connect(
            f"/ws/probe/{controller.run_id}",
            headers={"x-telnyx-streaming-auth-token": token},
        ) as ws:
            ws.send_text(start_raw("route-call"))
            ws.send_text(
                json.dumps(
                    {
                        "event": "error",
                        "sequence_number": "2",
                        "payload": {"code": 1, "title": "safe"},
                    }
                )
            )
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.receive_text()


def test_probe_route_both_tracks_rejects_queued_fixture_as_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = config(tmp_path)
    fixture, fixture_frames = patterned_fixture()
    cfg = BenchConfig(
        settings=base.settings,
        agent_number=base.agent_number,
        harness_number=base.harness_number,
        harness_connection_id=base.harness_connection_id,
        public_wss_base=base.public_wss_base,
        target_legs=base.target_legs,
        fixture=fixture,
        artifacts_root=tmp_path / "route-artifacts",
    )
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    analysis_calls = 0
    original_candidate = acoustic_probe._candidate_agent_track

    def counted_candidate(*args: Any, **kwargs: Any) -> Any:
        nonlocal analysis_calls
        analysis_calls += 1
        return original_candidate(*args, **kwargs)

    monkeypatch.setattr(acoustic_probe, "_candidate_agent_track", counted_candidate)
    active = array("h", [8_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.bridge_ready.set()
        token = _route_token(controller)
        with client.websocket_connect(
            f"/ws/probe/{controller.run_id}",
            headers={"x-telnyx-streaming-auth-token": token},
        ) as ws:
            ws.send_text(start_raw("route-call"))
            state = {
                "probe_sequence": 2,
                "agent_sequence": 2,
                "probe_chunk": 0,
                "agent_chunk": 0,
            }

            def send_pair(inbound: bytes, outbound: bytes) -> None:
                send_cross_leg_pair(ws, controller, inbound, outbound, state)

            for index in range(30):
                send_pair(active if index < 5 else silence, silence)
            assert analysis_calls == 3
            sent = []
            for _ in fixture_frames:
                sent.append(ws.receive_json())
                send_pair(silence, silence)
            assert all(message["event"] == "media" for message in sent)
            send_pair(silence, silence)
            assert ws.receive_json()["event"] == "mark"
            for index in range(104):
                queued = fixture_frames[index - 5] if 5 <= index < 9 else silence
                send_pair(queued, queued)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        assert controller.failure == "fixture_match_ambiguous"
        manifest = json.loads((controller.artifacts.path / "manifest.json").read_text())
        assert manifest["gate_outcome"] == "NO-GO"
        assert manifest["failure_category"] == "fixture_match_ambiguous"


@pytest.mark.parametrize(
    "mode", ["success", "overlap", "mixed_response", "lagging_mirror"]
)
def test_probe_route_requires_joint_silence_then_completes_lifecycle(
    tmp_path: Path, mode: str
) -> None:
    base = config(tmp_path)
    fixture, fixture_frames = patterned_fixture()
    cfg = BenchConfig(
        settings=base.settings,
        agent_number=base.agent_number,
        harness_number=base.harness_number,
        harness_connection_id=base.harness_connection_id,
        public_wss_base=base.public_wss_base,
        target_legs=base.target_legs,
        fixture=fixture,
        artifacts_root=tmp_path / f"joint-route-artifacts-{mode}",
    )
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    active = array("h", [8_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.leg_b = "memory-leg-b"
        controller.bridge_ready.set()
        token = _route_token(controller)
        with client.websocket_connect(
            f"/ws/probe/{controller.run_id}",
            headers={"x-telnyx-streaming-auth-token": token},
        ) as ws:
            ws.send_text(start_raw("route-call"))
            state = {
                "probe_sequence": 2,
                "agent_sequence": 2,
                "probe_chunk": 0,
                "agent_chunk": 0,
            }

            def send_pair(inbound: bytes, outbound: bytes) -> None:
                send_cross_leg_pair(ws, controller, inbound, outbound, state)

            for index in range(30):
                send_pair(active if index < 5 else silence, silence)
            for _ in fixture_frames:
                assert ws.receive_json()["event"] == "media"
                send_pair(silence, silence)
            send_pair(silence, silence)
            assert ws.receive_json()["event"] == "mark"
            for index in range(104):
                returned = fixture_frames[index - 5] if 5 <= index < 9 else silence
                crossing_agent = (
                    active if mode == "overlap" and 9 <= index < 14 else silence
                )
                send_pair(crossing_agent, returned)
            if mode != "overlap":
                for index in range(35):
                    response = active if index < 6 else silence
                    stimulus_response = (
                        response
                        if mode == "mixed_response"
                        else (
                            active
                            if mode == "lagging_mirror" and index == 32
                            else silence
                        )
                    )
                    send_pair(
                        response,
                        stimulus_response,
                    )
                    if (
                        controller.failure is not None
                        or ProbeState.CAPTURE_COMPLETED in controller.states
                    ):
                        break
            for _ in range(3_000):
                if (
                    controller.failure is not None
                    or ProbeState.CAPTURE_COMPLETED in controller.states
                ):
                    break
                time.sleep(0.001)
            else:
                sizes = (
                    {
                        key: len(value)
                        for key, value in controller.capture.tracks.items()
                    }
                    if controller.capture
                    else {}
                )
                raise AssertionError(
                    "joint lifecycle did not terminate: "
                    f"states={controller.states} "
                    f"sizes={sizes}"
                )
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        expected_failure = {
            "success": None,
            "overlap": "stimulus_overlap",
            "mixed_response": "track_ambiguous",
            "lagging_mirror": "track_ambiguous",
        }[mode]
        assert controller.failure == expected_failure
        assert (ProbeState.CAPTURE_COMPLETED in controller.states) is (
            mode == "success"
        )
        assert not controller.probe_socket_active
        manifest = json.loads((controller.artifacts.path / "manifest.json").read_text())
        assert manifest["gate_outcome"] == (
            "CAPTURE_COMPLETE_PENDING_REVIEW" if mode == "success" else "NO-GO"
        )
        assert manifest["teardown_result"] == "both_legs_hung_up"
        if mode == "success":
            summary = json.loads(
                (controller.artifacts.path / "detector_summary.json").read_text()
            )
            samples = {
                windows["D_post_stimulus_agent_response"]["sample_count"]
                for windows in summary["window_statistics"].values()
            }
            assert min(samples) > 0
            assert max(samples) - min(samples) <= 320


def test_agent_route_constructs_voice_agent_only_for_leg_b_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    constructed: list[str] = []
    closed: list[str] = []

    class FakeMediaStream:
        def __init__(self, ws: object, *, frame_ms: int, lead_frames: int) -> None:
            self.on_error: object = None

        def start(self) -> None:
            pass

        async def aclose(self) -> None:
            closed.append("media")

    class FakeVoiceAgent:
        run_task: None = None

        def __init__(self, *args: object) -> None:
            constructed.append("leg-b")
            self._on_socket_error = lambda: None

        def set_call_info(self, call_id: str, from_number: str) -> None:
            pass

        def start(self) -> None:
            pass

        def handle_audio(self, pcm16: bytes) -> None:
            pass

        def submit_speak_ended(self, generation: int | None) -> None:
            pass

        def submit_hangup(self) -> None:
            pass

        async def aclose(self) -> None:
            closed.append("agent")

    monkeypatch.setattr(acoustic_probe, "MediaStream", FakeMediaStream)
    monkeypatch.setattr(acoustic_probe, "VoiceAgent", FakeVoiceAgent)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.bridge_ready.set()
        controller.mark_media_ready(acoustic_probe.PROBE_CHANNEL, time.monotonic_ns())
        probe_token = _route_token(controller, "leg-a-call", "probe")
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/agent/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": probe_token},
            ) as ws,
        ):
            ws.receive_text()
        assert constructed == []
        token = _route_token(controller, "leg-b-call", "agent")
        with client.websocket_connect(
            f"/ws/agent/{controller.run_id}",
            headers={"x-telnyx-streaming-auth-token": token},
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "event": "start",
                        "sequence_number": "1",
                        "stream_id": "agent-stream",
                        "start": {
                            "call_control_id": "leg-b-call",
                            "from": "synthetic-source",
                            "media_format": {
                                "encoding": "L16",
                                "sample_rate": 16_000,
                                "channels": 1,
                            },
                        },
                    }
                )
            )
            ws.send_text(
                probe_media_raw(
                    sequence=2,
                    track="inbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x01\x00" * 320,
                )
            )
            ws.send_text(json.dumps({"event": "stop", "sequence_number": "3"}))
        assert controller.agent is None
        assert controller.capture is not None
        assert len(controller.capture.tracks[acoustic_probe.AGENT_CHANNEL]) == 640
    assert constructed == ["leg-b"]
    assert closed == ["agent", "media"]


def test_probe_and_agent_routes_capture_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    handled_audio: list[bytes] = []

    class FakeMediaStream:
        def __init__(self, ws: object, *, frame_ms: int, lead_frames: int) -> None:
            self.on_error: object = None

        def start(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class FakeVoiceAgent:
        run_task: None = None

        def __init__(self, *args: object) -> None:
            self._on_socket_error = lambda: None

        def set_call_info(self, call_id: str, from_number: str) -> None:
            pass

        def start(self) -> None:
            pass

        def handle_audio(self, pcm16: bytes) -> None:
            handled_audio.append(pcm16)

        def submit_speak_ended(self, generation: int | None) -> None:
            pass

        def submit_hangup(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(acoustic_probe, "MediaStream", FakeMediaStream)
    monkeypatch.setattr(acoustic_probe, "VoiceAgent", FakeVoiceAgent)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.bridge_ready.set()
        probe_token = _route_token(controller, "probe-call", "probe")
        agent_token = _route_token(controller, "agent-call", "agent")
        with (
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": probe_token},
            ) as probe_ws,
            client.websocket_connect(
                f"/ws/agent/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": agent_token},
            ) as agent_ws,
        ):
            probe_ws.send_text(start_raw("probe-call"))
            agent_ws.send_text(start_raw("agent-call"))
            agent_ws.send_text(
                probe_media_raw(
                    sequence=2,
                    track="outbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x03\x00" * 320,
                )
            )
            agent_ws.send_text(
                probe_media_raw(
                    sequence=3,
                    track="inbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x01\x00" * 320,
                )
            )
            agent_ws.send_text(
                probe_media_raw(
                    sequence=4,
                    track="outbound",
                    chunk=2,
                    timestamp=20,
                    pcm16=b"\x04\x00" * 320,
                )
            )
            agent_ws.send_text(
                probe_media_raw(
                    sequence=5,
                    track="inbound",
                    chunk=2,
                    timestamp=20,
                    pcm16=b"\x05\x00" * 320,
                )
            )
            agent_ws.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "sequence_number": "6",
                        "mark": {"name": "diagnostic"},
                    }
                )
            )
            probe_ws.send_text(
                probe_media_raw(
                    sequence=2,
                    track="inbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x06\x00" * 320,
                )
            )
            probe_ws.send_text(
                probe_media_raw(
                    sequence=3,
                    track="outbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x02\x00" * 320,
                )
            )
            agent_ws.send_text(
                json.dumps({"event": "stop", "sequence_number": "7"})
            )
            for _ in range(1_000):
                capture = controller.capture
                if (
                    capture is not None
                    and all(capture.tracks.values())
                    and len(handled_audio) == 2
                ):
                    break
                time.sleep(0.001)
            else:
                raise AssertionError("both live routes did not append media")
            assert controller.measurement_ready.is_set()
            assert controller.measurement_start_ns is not None
            assert bytes(capture.tracks[acoustic_probe.AGENT_CHANNEL]) == (
                b"\x01\x00" * 320 + b"\x05\x00" * 320
            )
            assert bytes(capture.tracks[acoustic_probe.PROBE_CHANNEL]) == (
                b"\x02\x00" * 320
            )
            assert handled_audio == [b"\x01\x00" * 320, b"\x05\x00" * 320]
            with pytest.raises(WebSocketDisconnect):
                agent_ws.receive_text()
            assert controller.agent_integrity is not None
            assert not controller.agent_integrity.ordering.unresolved
            assert controller.probe_integrity is not None
            assert not controller.probe_integrity.ordering.unresolved
            assert not capture.ordering.unresolved


def test_probe_both_tracks_measures_only_outbound_with_global_integrity(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    app = create_app(cfg, cast("SafeCallControl", fake))
    inbound = b"\x06\x00" * 320
    outbound_one = b"\x02\x00" * 320
    outbound_two = b"\x03\x00" * 320
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.leg_b = "memory-leg-b"
        controller.bridge_ready.set()
        controller.agent_integrity = BoundedCapture(
            max_bytes_per_track=1_000_000, max_event_rows=100
        )
        token = _route_token(controller, "probe-call", "probe")
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.send_text(start_raw("probe-call"))
            controller.mark_media_ready(
                acoustic_probe.AGENT_CHANNEL, time.monotonic_ns()
            )
            for sequence, track, chunk, payload in (
                (2, "inbound", 1, inbound),
                (3, "outbound", 1, outbound_one),
                (4, "inbound", 2, inbound),
                (5, "outbound", 2, outbound_two),
            ):
                ws.send_text(
                    probe_media_raw(
                        sequence=sequence,
                        track=track,
                        chunk=chunk,
                        timestamp=(chunk - 1) * 20,
                        pcm16=payload,
                    )
                )
            ws.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "sequence_number": "6",
                        "mark": {"name": "diagnostic"},
                    }
                )
            )
            ws.send_text(json.dumps({"event": "stop", "sequence_number": "7"}))
            ws.receive_text()
        assert controller.failure == "agent_audio_not_observed"
        assert controller.probe_integrity is not None
        assert not controller.probe_integrity.ordering.unresolved
        assert controller.capture is not None
        assert bytes(controller.capture.tracks[acoustic_probe.PROBE_CHANNEL]) == (
            outbound_one + outbound_two
        )
        assert inbound not in bytes(
            controller.capture.tracks[acoustic_probe.PROBE_CHANNEL]
        )
        assert not controller.capture.ordering.unresolved
        assert not (controller.artifacts.path / "send_frames.jsonl").exists()


def test_energetic_probe_inbound_alone_cannot_select_greeting(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    app = create_app(cfg, cast("SafeCallControl", fake))
    active = array("h", [8_000] * 320).tobytes()
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.leg_b = "memory-leg-b"
        controller.bridge_ready.set()
        controller.agent_integrity = BoundedCapture(
            max_bytes_per_track=1_000_000, max_event_rows=100
        )
        token = _route_token(controller, "probe-call", "probe")
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.send_text(start_raw("probe-call"))
            controller.mark_media_ready(
                acoustic_probe.AGENT_CHANNEL, time.monotonic_ns()
            )
            controller.mark_greeting_started()
            for index in range(10):
                ws.send_text(
                    probe_media_raw(
                        sequence=index + 2,
                        track="inbound",
                        chunk=index + 1,
                        timestamp=index * 20,
                        pcm16=active,
                    )
                )
            ws.send_text(json.dumps({"event": "stop", "sequence_number": "12"}))
            ws.receive_text()
        assert controller.failure == "agent_audio_not_observed"
        assert controller.capture is not None
        assert not controller.capture.tracks[acoustic_probe.PROBE_CHANNEL]
        assert not (controller.artifacts.path / "send_frames.jsonl").exists()


def test_probe_route_capture_limit_is_named_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr(acoustic_probe, "MAX_CAPTURE_BYTES_PER_TRACK", 1)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.bridge_ready.set()
        token = _route_token(controller)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.send_text(start_raw("route-call"))
            ws.send_text(
                probe_media_raw(
                    sequence=2,
                    track="outbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x00\x00",
                )
            )
            ws.receive_text()
        assert controller.failure == "capture_limit_reached"


def test_failure_waveform_error_cannot_block_probe_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    fake = FakeCallControl(cfg.settings)
    app = create_app(cfg, cast("SafeCallControl", fake))
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.leg_a = "memory-leg-a"
        controller.leg_b = "memory-leg-b"
        controller.bridge_ready.set()
        token = _route_token(controller)

        def fail_write() -> None:
            raise OSError("simulated diagnostic disk failure")

        monkeypatch.setattr(controller, "write_failure_waveforms", fail_write)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/ws/probe/{controller.run_id}",
                headers={"x-telnyx-streaming-auth-token": token},
            ) as ws,
        ):
            ws.send_text(start_raw("route-call"))
            ws.send_text(
                json.dumps(
                    {
                        "event": "error",
                        "sequence_number": "2",
                        "payload": {"code": 1, "title": "safe"},
                    }
                )
            )
            ws.receive_text()
        assert not controller.probe_socket_active
        assert controller.teardown_result == "both_legs_hung_up"
        assert {(ccid, action) for ccid, action, _ in fake.actions} >= {
            ("memory-leg-a", "hangup"),
            ("memory-leg-b", "hangup"),
        }


def test_live_preflight_checks_directory_form_of_anchored_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    class Result:
        returncode = 0

    def record(command: list[str], *, check: bool, cwd: Path) -> Result:
        commands.append(command)
        return Result()

    monkeypatch.setattr("bench.acoustic_probe.subprocess.run", record)
    acoustic_probe._run_live_preflight()
    ignore_command = commands[0]
    assert ignore_command[:3] == ["git", "check-ignore", "-q"]
    assert ignore_command[3] == f"{acoustic_probe.ARTIFACT_ROOT}{os.sep}"


def test_completed_stimulus_task_failure_is_retrieved_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = config(tmp_path)
    cfg = BenchConfig(
        settings=base.settings,
        agent_number=base.agent_number,
        harness_number=base.harness_number,
        harness_connection_id=base.harness_connection_id,
        public_wss_base=base.public_wss_base,
        target_legs=base.target_legs,
        fixture=base.fixture,
        artifacts_root=tmp_path / "failed-stimulus-artifacts",
        capture_seconds=1,
    )

    async def fail_send(*args: object, **kwargs: object) -> PacingSummary:
        raise RuntimeError("synthetic send failure")

    monkeypatch.setattr(acoustic_probe, "send_fixture_paced", fail_send)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    active = array("h", [8_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    with TestClient(app) as client:
        controller: ProbeController = app.state.controller
        controller.bridge_ready.set()
        token = _route_token(controller)
        with client.websocket_connect(
            f"/ws/probe/{controller.run_id}",
            headers={"x-telnyx-streaming-auth-token": token},
        ) as ws:
            ws.send_text(start_raw("route-call"))
            state = {
                "probe_sequence": 2,
                "agent_sequence": 2,
                "probe_chunk": 0,
                "agent_chunk": 0,
            }
            for index in range(30):
                send_cross_leg_pair(
                    ws,
                    controller,
                    active if index < 5 else silence,
                    silence,
                    state,
                )
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        assert controller.failure == "stimulus_send_failed"


def test_fixture_reference_search_follows_delayed_returned_audio() -> None:
    frame = 320
    amplitudes = (500, 2_000, 8_000, 1_000)
    fixture_pcm = array(
        "h", [value for amplitude in amplitudes for value in [amplitude] * frame]
    ).tobytes()
    fixture = Fixture(
        pcm16=fixture_pcm,
        sha256="f" * 64,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        frame_bytes=640,
        first_active_sample=0,
        onset_method="test",
        onset_threshold=1,
    )
    queued = b"\x00\x00" * (frame * 3) + fixture_pcm
    match = match_fixture_reference(queued, fixture)
    assert match is not None
    assert match.alignment_frames == 3
    assert match.end_byte == len(queued)


def test_joint_track_selection_waits_for_common_interval_and_rejects_twins() -> None:
    capture = BoundedCapture(max_bytes_per_track=1_000_000, max_event_rows=100)
    active = b"\x40\x1f" * 320
    silence = b"\x00\x00" * 320
    sequence = 1
    for index in range(30):
        payload = active if index < 5 else silence
        capture.append(
            MediaFrame(
                sequence, "stream", "inbound", index + 1, index * 20, payload, sequence
            )
        )
        sequence += 1
        if index < 29:
            capture.append(
                MediaFrame(
                    sequence,
                    "stream",
                    "outbound",
                    index + 1,
                    index * 20,
                    payload,
                    sequence,
                )
            )
            sequence += 1
    candidate, _, ambiguous = acoustic_probe._candidate_agent_track(
        capture, DetectorConfig()
    )
    assert candidate is None and not ambiguous
    capture.append(
        MediaFrame(sequence, "stream", "outbound", 30, 580, silence, sequence)
    )
    candidate, _, ambiguous = acoustic_probe._candidate_agent_track(
        capture, DetectorConfig()
    )
    assert candidate is None and ambiguous


def test_host_alignment_ignores_asymmetric_channel_prefixes() -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=1_000_000,
        max_event_rows=1_000,
    )
    active = array("h", [8_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    base = 1_000_000_000
    sequence = {acoustic_probe.PROBE_CHANNEL: 1, acoustic_probe.AGENT_CHANNEL: 1}

    def append(channel: str, payload: bytes, receive_ns: int) -> None:
        current = sequence[channel]
        capture.append(
            channel,
            MediaFrame(
                current,
                channel,
                "inbound",
                current,
                (current - 1) * 20,
                payload,
                receive_ns,
            ),
        )
        sequence[channel] += 1

    for index in range(3):
        append(acoustic_probe.PROBE_CHANNEL, silence, base + index * 20_000_000)
    for index in range(32):
        receive_ns = base + (index + 3) * 20_000_000
        append(
            acoustic_probe.PROBE_CHANNEL,
            active if index < 6 else silence,
            receive_ns,
        )
        append(acoustic_probe.AGENT_CHANNEL, silence, receive_ns + 5_000_000)

    ranges = acoustic_probe._aligned_host_window(capture, base)
    assert ranges is not None
    assert ranges[acoustic_probe.PROBE_CHANNEL][0] == 4 * 640
    assert ranges[acoustic_probe.AGENT_CHANNEL][0] == 0
    candidate, _, ambiguous = acoustic_probe._candidate_agent_track(
        capture, DetectorConfig(), start_ns=base
    )
    assert candidate == acoustic_probe.PROBE_CHANNEL
    assert not ambiguous


def test_continuously_active_probe_leg_never_establishes_natural_stop() -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=1_000_000,
        max_event_rows=2_000,
    )
    active = array("h", [8_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    for index in range(750):
        for channel, payload in (
            (acoustic_probe.PROBE_CHANNEL, active),
            (acoustic_probe.AGENT_CHANNEL, silence),
        ):
            capture.append(
                channel,
                MediaFrame(
                    index + 1,
                    channel,
                    "inbound",
                    index + 1,
                    index * 20,
                    payload,
                    1_000_000_000 + index * 20_000_000,
                ),
            )
    candidate, _, ambiguous = acoustic_probe._candidate_agent_track(
        capture, DetectorConfig(), start_ns=1_000_000_000
    )
    assert candidate is None
    assert not ambiguous


def test_host_alignment_bounds_delayed_and_missing_channel_coverage() -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=100_000,
        max_event_rows=100,
    )
    silence = b"\x00\x00" * 320
    for index in range(8):
        for channel, delay_ns in (
            (acoustic_probe.PROBE_CHANNEL, 0),
            (acoustic_probe.AGENT_CHANNEL, 15_000_000),
        ):
            capture.append(
                channel,
                MediaFrame(
                    index + 1,
                    channel,
                    "inbound",
                    index + 1,
                    index * 20,
                    silence,
                    1_000_000_000 + index * 20_000_000 + delay_ns,
                ),
            )
    assert (
        acoustic_probe._aligned_host_window(
            capture, 1_030_000_000, 1_100_000_000, strict=True
        )
        is not None
    )

    delayed = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=100_000,
        max_event_rows=100,
    )
    for channel, receive_ns in (
        (acoustic_probe.PROBE_CHANNEL, 1_000_000_000),
        (acoustic_probe.PROBE_CHANNEL, 1_100_000_000),
        (acoustic_probe.AGENT_CHANNEL, 1_050_000_000),
        (acoustic_probe.AGENT_CHANNEL, 1_150_000_000),
    ):
        delayed.append(
            channel,
            MediaFrame(1, channel, "inbound", 1, 0, silence, receive_ns),
        )
    with pytest.raises(ProbeProtocolError) as exc_info:
        acoustic_probe._aligned_host_window(delayed, 1_000_000_000, strict=True)
    assert exc_info.value.category == "cross_channel_alignment_failed"

    missing = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=100_000,
        max_event_rows=100,
    )
    missing.append(
        acoustic_probe.PROBE_CHANNEL,
        MediaFrame(1, "probe", "inbound", 1, 0, silence, 1_000_000_000),
    )
    with pytest.raises(ProbeProtocolError):
        acoustic_probe._aligned_host_window(missing, 1_000_000_000, strict=True)

    correlated_stall = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=100_000,
        max_event_rows=100,
    )
    for channel, times in (
        (acoustic_probe.PROBE_CHANNEL, (1_000_000_000, 1_100_000_000)),
        (acoustic_probe.AGENT_CHANNEL, (1_005_000_000, 1_105_000_000)),
    ):
        for sequence, receive_ns in enumerate(times, start=1):
            correlated_stall.append(
                channel,
                MediaFrame(
                    sequence,
                    channel,
                    "inbound",
                    sequence,
                    (sequence - 1) * 20,
                    silence,
                    receive_ns,
                ),
            )
    with pytest.raises(ProbeProtocolError) as exc_info:
        acoustic_probe._aligned_host_window(
            correlated_stall, 1_040_000_000, strict=True
        )
    assert exc_info.value.category == "cross_channel_alignment_failed"


def test_dynamic_common_end_waits_for_transient_handler_gap() -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=100_000,
        max_event_rows=100,
    )
    silence = b"\x00\x00" * 320

    def append(channel: str, sequence: int, receive_ms: int) -> None:
        capture.append(
            channel,
            MediaFrame(
                sequence,
                channel,
                "inbound",
                sequence,
                (sequence - 1) * 20,
                silence,
                1_000_000_000 + receive_ms * 1_000_000,
            ),
        )

    for sequence, receive_ms in enumerate((0, 20, 140), start=1):
        append(acoustic_probe.PROBE_CHANNEL, sequence, receive_ms)
    for sequence, receive_ms in enumerate((0, 20, 40, 60, 80), start=1):
        append(acoustic_probe.AGENT_CHANNEL, sequence, receive_ms)

    assert acoustic_probe._aligned_host_window(capture, 1_000_000_000) is None
    with pytest.raises(ProbeProtocolError):
        acoustic_probe._aligned_host_window(capture, 1_000_000_000, strict=True)
    with pytest.raises(ProbeProtocolError) as exc_info:
        acoustic_probe._aligned_host_window(
            capture,
            1_000_000_000,
            1_080_000_000,
            strict=True,
        )
    assert exc_info.value.category == "cross_channel_alignment_failed"

    append(acoustic_probe.AGENT_CHANNEL, 6, 120)
    assert acoustic_probe._aligned_host_window(capture, 1_000_000_000) is not None


@pytest.mark.parametrize(
    ("receive_period_ns", "valid"),
    [(20_000_000, True), (1_000_000, True), (40_000_000, False)],
)
def test_fixture_separation_excludes_energetic_final_frame(
    receive_period_ns: int,
    valid: bool,
) -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=1_000_000,
        max_event_rows=1_000,
    )
    energetic = array("h", [1_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    base = 1_000_000_000
    fixture_frames = 4
    total_frames = fixture_frames + 110
    for index in range(total_frames):
        for channel, delay_ns in (
            (acoustic_probe.PROBE_CHANNEL, 0),
            (acoustic_probe.AGENT_CHANNEL, 100_000),
        ):
            capture.append(
                channel,
                MediaFrame(
                    index + 1,
                    channel,
                    "inbound",
                    index + 1,
                    index * 20,
                    energetic if index < fixture_frames else silence,
                    base + index * receive_period_ns + delay_ns,
                ),
            )
    fixture_end_byte = fixture_frames * 640
    fixture_end_receive_ns = base + (fixture_frames - 1) * receive_period_ns
    if not valid:
        with pytest.raises(ProbeProtocolError) as exc_info:
            acoustic_probe._fixture_separation_ranges(
                capture,
                acoustic_probe.PROBE_CHANNEL,
                fixture_end_byte,
                fixture_end_receive_ns,
            )
        assert exc_info.value.category == "stimulus_boundary_ambiguous"
        return
    result = acoustic_probe._fixture_separation_ranges(
        capture,
        acoustic_probe.PROBE_CHANNEL,
        fixture_end_byte,
        fixture_end_receive_ns,
    )
    assert result is not None
    ranges, _ = result
    assert ranges[acoustic_probe.PROBE_CHANNEL][0] == fixture_end_byte
    for track, (start, end) in ranges.items():
        assert end - start >= 3_200
        assert acoustic_probe._rms_dbfs(bytes(capture.tracks[track][start:end])) == (
            -float("inf")
        )


def test_cadenced_greeting_analysis_preserves_boundary_with_bounded_delay() -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=1_000_000,
        max_event_rows=1_000,
    )
    active = array("h", [8_000] * 320).tobytes()
    silence = b"\x00\x00" * 320
    base = 1_000_000_000
    earliest: tuple[int, Any] | None = None
    cadenced: tuple[int, Any] | None = None
    for index in range(40):
        for channel, delay_ns, payload in (
            (acoustic_probe.AGENT_CHANNEL, 1_000_000, silence),
            (
                acoustic_probe.PROBE_CHANNEL,
                0,
                active if 1 <= index < 6 else silence,
            ),
        ):
            capture.append(
                channel,
                MediaFrame(
                    index + 1,
                    channel,
                    "inbound",
                    index + 1,
                    index * 20,
                    payload,
                    base + index * 20_000_000 + delay_ns,
                ),
            )
        candidate, analyses, _ = acoustic_probe._candidate_agent_track(
            capture, DetectorConfig(), start_ns=base
        )
        if candidate is not None and earliest is None:
            earliest = (index + 1, analyses[candidate].result)
        if (index + 1) % acoustic_probe.GREETING_ANALYSIS_INTERVAL_FRAMES == 0:
            candidate, analyses, _ = acoustic_probe._candidate_agent_track(
                capture, DetectorConfig(), start_ns=base
            )
            if candidate is not None and cadenced is None:
                cadenced = (index + 1, analyses[candidate].result)
    assert earliest is not None and cadenced is not None
    assert cadenced[0] - earliest[0] <= (
        acoustic_probe.GREETING_ANALYSIS_INTERVAL_FRAMES - 1
    )
    assert cadenced[1] == earliest[1]


def test_greeting_analysis_work_is_bounded_at_full_call_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = CrossLegCapture(
        (acoustic_probe.PROBE_CHANNEL, acoustic_probe.AGENT_CHANNEL),
        max_bytes_per_channel=4_000_000,
        max_event_rows=7_000,
    )
    silence = b"\x00\x00" * 320
    base = 1_000_000_000
    deadline = base + acoustic_probe.MAX_GREETING_ANALYSIS_SECONDS * 1_000_000_000
    analyzed_bytes: list[int] = []
    original = analyze_acoustic_stop

    def counted_analysis(pcm16: bytes, detector: DetectorConfig) -> Any:
        analyzed_bytes.append(len(pcm16))
        return original(pcm16, detector)

    monkeypatch.setattr(acoustic_probe, "analyze_acoustic_stop", counted_analysis)
    started = time.perf_counter()
    for index in range(3_000):
        for channel, delay_ns in (
            (acoustic_probe.AGENT_CHANNEL, 1_000_000),
            (acoustic_probe.PROBE_CHANNEL, 0),
        ):
            capture.append(
                channel,
                MediaFrame(
                    index + 1,
                    channel,
                    "inbound",
                    index + 1,
                    index * 20,
                    silence,
                    base + index * 20_000_000 + delay_ns,
                ),
            )
        frame_count = index + 1
        if frame_count <= 760 and (
            frame_count % acoustic_probe.GREETING_ANALYSIS_INTERVAL_FRAMES == 0
        ):
            receive_ns = base + index * 20_000_000
            acoustic_probe._candidate_agent_track(
                capture,
                DetectorConfig(),
                start_ns=base,
                end_ns=deadline if receive_ns >= deadline else None,
            )
    elapsed = time.perf_counter() - started
    assert len(analyzed_bytes) == 152
    assert max(analyzed_bytes) <= (
        acoustic_probe.MAX_GREETING_ANALYSIS_SECONDS * 16_000 * 2
    )
    assert elapsed < 2.0


def test_sanitized_frame_metadata_has_energy_but_no_pcm() -> None:
    payload = array("h", [0, 1_000, -32_768, 32_767]).tobytes()
    frame = MediaFrame(2, "stream", "inbound", 1, 0, payload, 123)
    metadata = acoustic_probe._frame_metadata(frame, channel="channel")
    assert metadata["payload_bytes"] == len(payload)
    assert metadata["rms_dbfs"] == pytest.approx(acoustic_probe._rms_dbfs(payload))
    assert metadata["peak_abs"] == 32_768
    assert metadata["clipped_samples"] == 2
    serialized = json.dumps(metadata)
    assert "pcm16" not in metadata
    assert "payload" not in metadata
    assert base64.b64encode(payload).decode() not in serialized
