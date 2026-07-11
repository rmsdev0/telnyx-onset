"""Fixture, pacing, and isolated controller tests; all network-free."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import wave
from array import array
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bench.acoustic_probe as acoustic_probe
from bench.acoustic_probe import (
    BenchConfig,
    Fixture,
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
from bench.acoustic_stop import DetectorConfig
from bench.media_capture import TOKEN_TTL_NS, BoundedCapture, MediaFrame
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


def test_probe_route_stalled_auth_times_out_before_capture_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    app = create_app(cfg, cast("SafeCallControl", FakeCallControl(cfg.settings)))
    allocations = 0
    original = BoundedCapture

    def counted_capture(
        *, max_bytes_per_track: int, max_event_rows: int
    ) -> BoundedCapture:
        nonlocal allocations
        allocations += 1
        return original(
            max_bytes_per_track=max_bytes_per_track, max_event_rows=max_event_rows
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
    tmp_path: Path,
) -> None:
    base = config(tmp_path)
    fixture, fixture_frames = patterned_fixture()
    cfg = BenchConfig(
        settings=base.settings,
        agent_number=base.agent_number,
        harness_number=base.harness_number,
        public_wss_base=base.public_wss_base,
        target_legs=base.target_legs,
        fixture=fixture,
        artifacts_root=tmp_path / "route-artifacts",
    )
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
            sequence = 2
            chunks = {"inbound": 0, "outbound": 0}

            def send_pair(inbound: bytes, outbound: bytes) -> None:
                nonlocal sequence
                for track, payload in (
                    ("inbound", inbound),
                    ("outbound", outbound),
                ):
                    chunks[track] += 1
                    ws.send_text(
                        probe_media_raw(
                            sequence=sequence,
                            track=track,
                            chunk=chunks[track],
                            timestamp=(chunks[track] - 1) * 20,
                            pcm16=payload,
                        )
                    )
                    sequence += 1

            for index in range(30):
                send_pair(active if index < 5 else silence, silence)
            sent = [ws.receive_json() for _ in fixture_frames]
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
            sequence = 2
            chunks = {"inbound": 0, "outbound": 0}

            def send_pair(inbound: bytes, outbound: bytes) -> None:
                nonlocal sequence
                for track, payload in (
                    ("inbound", inbound),
                    ("outbound", outbound),
                ):
                    chunks[track] += 1
                    ws.send_text(
                        probe_media_raw(
                            sequence=sequence,
                            track=track,
                            chunk=chunks[track],
                            timestamp=(chunks[track] - 1) * 20,
                            pcm16=payload,
                        )
                    )
                    sequence += 1

            for index in range(30):
                send_pair(active if index < 5 else silence, silence)
            assert all(ws.receive_json()["event"] == "media" for _ in fixture_frames)
            send_pair(silence, silence)
            assert ws.receive_json()["event"] == "mark"
            for index in range(104):
                returned = fixture_frames[index - 5] if 5 <= index < 9 else silence
                crossing_agent = (
                    active if mode == "overlap" and 9 <= index < 14 else silence
                )
                send_pair(crossing_agent, returned)
            if mode != "overlap":
                for index in range(30):
                    response = active if index < 5 else silence
                    stimulus_response = (
                        response
                        if mode == "mixed_response"
                        else (
                            active
                            if mode == "lagging_mirror" and index == 29
                            else silence
                        )
                    )
                    send_pair(
                        response,
                        stimulus_response,
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
            frames = {
                windows["D_post_stimulus_agent_response"]["frame_count"]
                for windows in summary["window_statistics"].values()
            }
            assert len(frames) == 1


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
                        "stream_id": "agent-stream",
                        "start": {
                            "call_control_id": "leg-b-call",
                            "from": "synthetic-source",
                        },
                    }
                )
            )
            ws.send_text(json.dumps({"event": "stop"}))
        assert controller.agent is None
    assert constructed == ["leg-b"]
    assert closed == ["agent", "media"]


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
                    track="inbound",
                    chunk=1,
                    timestamp=0,
                    pcm16=b"\x00\x00",
                )
            )
            ws.receive_text()
        assert controller.failure == "capture_limit_reached"


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
            sequence = 2
            for index in range(30):
                for track, payload in (
                    ("inbound", active if index < 5 else silence),
                    ("outbound", silence),
                ):
                    ws.send_text(
                        probe_media_raw(
                            sequence=sequence,
                            track=track,
                            chunk=index + 1,
                            timestamp=index * 20,
                            pcm16=payload,
                        )
                    )
                    sequence += 1
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
