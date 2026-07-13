"""pjsua2 CallMedia adapter for the SIP harness (spec §2, §12).

This module is deliberately thin: every methodology decision lives in the
offline-tested SipSession. It is imported only by the live CLI and exercised
only in the bounded live attempt, so it carries no offline test coverage by
design (spec §12). pjsua2 has no type stubs and is not an offline dependency.
"""

from __future__ import annotations

import platform
import sys
import threading
import time
from typing import TYPE_CHECKING

from bench.sip_harness import (
    PCM16_8K_FRAME_BYTES,
    RxFrame,
    decode_pcmu_to_pcm16_8k,
    encode_pcm16_to_pcmu,
)

if TYPE_CHECKING:
    from bench.sip_harness import SipSession

import pjsua2 as pj

STATS_POLL_NS = 500_000_000
EVENT_POLL_MS = 20
DISCONNECT_WAIT_S = 10


class _HarnessPort(pj.AudioMediaPort):  # type: ignore[misc]
    """Pull/push port on the stack's 8 kHz media clock."""

    def __init__(self, bridge: _CallBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def onFrameRequested(self, frame: object) -> None:  # noqa: N802
        pcm = self._bridge.pull_tx_pcm16()
        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO  # type: ignore[attr-defined]
        buf = pj.ByteVector()
        for byte in pcm:
            buf.append(byte)
        frame.buf = buf  # type: ignore[attr-defined]
        frame.size = len(pcm)  # type: ignore[attr-defined]

    def onFrameReceived(self, frame: object) -> None:  # noqa: N802
        data = bytes(frame.buf)[: frame.size]  # type: ignore[attr-defined]
        self._bridge.push_rx_pcm16(data)


class _Call(pj.Call):  # type: ignore[misc]
    def __init__(self, account: pj.Account, bridge: _CallBridge) -> None:
        super().__init__(account)
        self._bridge = bridge

    def onCallState(self, prm: object) -> None:  # noqa: N802
        info = self.getInfo()
        if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
            codec = "unknown"
            try:
                stream = self.getStreamInfo(0)
                codec = str(stream.codecName)
            except pj.Error:
                pass
            self._bridge.on_answered(codec)
        elif info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._bridge.on_disconnected()

    def onCallMediaState(self, prm: object) -> None:  # noqa: N802
        info = self.getInfo()
        for index, media in enumerate(info.media):
            if (
                media.type == pj.PJMEDIA_TYPE_AUDIO
                and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE
            ):
                audio = self.getAudioMedia(index)
                self._bridge.attach_media(audio)


class _CallBridge:
    """Serializes stack callbacks into the single-threaded SipSession."""

    def __init__(self, session: SipSession, port: _HarnessPort | None) -> None:
        self.session = session
        self.port: _HarnessPort | None = port
        self.lock = threading.Lock()
        self.answered = False
        self.remote_disconnected = False
        self.local_hangup_sent = False
        self._rx_sequence = 0
        self._rx_timestamp = 0

    @staticmethod
    def _now_ns() -> int:
        return time.monotonic_ns()

    def on_answered(self, codec: str) -> None:
        with self.lock:
            self.answered = True
            self.session.handle_answered(codec, self._now_ns())

    def on_disconnected(self) -> None:
        with self.lock:
            self.remote_disconnected = True
            if not self.local_hangup_sent:
                self.session.handle_remote_bye(self._now_ns())

    def attach_media(self, audio: pj.AudioMedia) -> None:
        if self.port is not None:
            self.port.startTransmit(audio)
            audio.startTransmit(self.port)

    def pull_tx_pcm16(self) -> bytes:
        with self.lock:
            pcmu = self.session.pull_tx_frame(self._now_ns())
        # The stack encodes the conference-bridge PCM back to G.711 for the
        # wire; mu-law is a bijection on its own codebook, so handing it the
        # expansion of our PCMU frame reproduces the exact wire bytes.
        return decode_pcmu_to_pcm16_8k(pcmu)

    def push_rx_pcm16(self, pcm16: bytes) -> None:
        if len(pcm16) != PCM16_8K_FRAME_BYTES:
            # Partial conference frames are stack artifacts, not media.
            return
        with self.lock:
            self._rx_sequence += 1
            self._rx_timestamp += len(pcm16) // 2
            self.session.handle_rx_frame(
                RxFrame(
                    pcmu=encode_pcm16_to_pcmu(pcm16),
                    rtp_sequence=self._rx_sequence,
                    rtp_timestamp=self._rx_timestamp * 0 + self._rx_sequence * 160,
                    host_receive_monotonic_ns=self._now_ns(),
                )
            )


def run_live_call(
    session: SipSession,
    *,
    sip_username: str,
    sip_password: str,
    sip_domain: str,
    caller_id: str,
    agent_number: str,
) -> None:
    """Place the bounded call and drive the session to a terminal outcome."""
    ep = pj.Endpoint()
    ep.libCreate()
    ep_cfg = pj.EpConfig()
    # Spec §9: SIP message tracing off; nothing routed into artifacts.
    ep_cfg.logConfig.level = 1
    ep_cfg.logConfig.consoleLevel = 1
    ep_cfg.logConfig.msgLogging = 0
    ep.libInit(ep_cfg)

    transport_cfg = pj.TransportConfig()
    transport_cfg.port = 0
    ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, transport_cfg)
    ep.libStart()
    ep.audDevManager().setNullDev()

    # Pin PCMU: every other codec priority to zero.
    for codec in ep.codecEnum2():
        priority = 255 if str(codec.codecId).startswith("PCMU/8000") else 0
        ep.codecSetPriority(codec.codecId, priority)

    acc_cfg = pj.AccountConfig()
    acc_cfg.idUri = f"sip:{caller_id}@{sip_domain}"
    acc_cfg.regConfig.registerOnAdd = False
    cred = pj.AuthCredInfo("digest", "*", sip_username, 0, sip_password)
    acc_cfg.sipConfig.authCreds.append(cred)
    account = pj.Account()
    account.create(acc_cfg)

    fmt = pj.MediaFormatAudio()
    fmt.type = pj.PJMEDIA_TYPE_AUDIO
    fmt.clockRate = 8_000
    fmt.channelCount = 1
    fmt.bitsPerSample = 16
    fmt.frameTimeUsec = 20_000
    bridge = _CallBridge(session, None)
    port = _HarnessPort(bridge)
    port.createPort("bench-harness-port", fmt)
    bridge.port = port

    call = _Call(account, bridge)
    call_prm = pj.CallOpParam(True)
    teardown_result = "hangup_sent"
    last_stats_ns = time.monotonic_ns()
    known_loss = 0
    delivery: dict[str, object] = {}
    try:
        call.makeCall(f"sip:{agent_number}@{sip_domain};transport=tls", call_prm)
        while True:
            ep.libHandleEvents(EVENT_POLL_MS)
            now_ns = time.monotonic_ns()
            with bridge.lock:
                session.tick(now_ns)
                outcome = session.outcome
            if bridge.remote_disconnected:
                outcome = outcome or "call_hangup"
            if now_ns - last_stats_ns >= STATS_POLL_NS and call.isActive():
                last_stats_ns = now_ns
                try:
                    stat = call.getStreamStat(0)
                    loss = int(stat.rtcp.rxStat.loss)
                    if loss > known_loss:
                        with bridge.lock:
                            session.report_rx_loss(loss - known_loss, now_ns)
                        known_loss = loss
                    delivery = {
                        "tx_packets": int(stat.rtcp.txStat.pkt),
                        "tx_bytes": int(stat.rtcp.txStat.bytes),
                        "rx_packets": int(stat.rtcp.rxStat.pkt),
                        "rx_loss": loss,
                        "rtt_estimate_ms": float(stat.rtcp.rttUsec.mean) / 1_000.0
                        if stat.rtcp.rttUsec.n
                        else None,
                    }
                except pj.Error:
                    pass
            if outcome is not None:
                break
    finally:
        with bridge.lock:
            bridge.local_hangup_sent = True
        if call.isActive():
            try:
                call.hangup(pj.CallOpParam(True))
            except pj.Error:
                teardown_result = "hangup_failed"
            deadline = time.monotonic() + DISCONNECT_WAIT_S
            while not bridge.remote_disconnected and time.monotonic() < deadline:
                ep.libHandleEvents(EVENT_POLL_MS)
            if not bridge.remote_disconnected:
                teardown_result = "hangup_unconfirmed"
        with bridge.lock:
            session.set_delivery_counters(delivery)
            session.finalize(
                teardown_result=teardown_result,
                now_ns=time.monotonic_ns(),
                extra={
                    "pjsua2_version": str(ep.libVersion().full),
                    "python_version": sys.version.split()[0],
                    "os_version": platform.platform(),
                    "sip_transport": "tls",
                    "greeting_tts_decode_mode": "whole_buffer",
                },
            )
        ep.libDestroy()
