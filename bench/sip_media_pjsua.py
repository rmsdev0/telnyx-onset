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
    fast_encode_pcm16_to_pcmu,
)

if TYPE_CHECKING:
    from bench.sip_harness import SipSession

import pjsua2 as pj

# Loss localization granularity: each poll interval is the void unit, so a
# lost packet costs at most this much certified timeline (plus run resets).
STATS_POLL_NS = 100_000_000
EVENT_POLL_MS = 20
DISCONNECT_WAIT_S = 10
# Pinned jitter-buffer depth (spec §3): fixed, recorded, and used to pad
# void intervals — the loss counter moves at packet arrival while the
# concealed audio surfaces roughly one buffer depth later.
JITTER_BUFFER_MS = 60
VOID_END_PAD_NS = (JITTER_BUFFER_MS + 20) * 1_000_000


class _HarnessPort(pj.AudioMediaPort):  # type: ignore[misc]
    """Pull/push port on the stack's 8 kHz media clock."""

    def __init__(self, bridge: _CallBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def onFrameRequested(self, frame: object) -> None:  # noqa: N802
        buf, size = self._bridge.pull_tx_buffer()
        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO  # type: ignore[attr-defined]
        frame.buf = buf  # type: ignore[attr-defined]
        frame.size = size  # type: ignore[attr-defined]

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
        # The tx alphabet is tiny (silence plus the fixture frames), so each
        # unique PCMU frame maps to one prebuilt ByteVector: no per-frame
        # conversion or SWIG-element loop on the media clock (attempt 2).
        self._tx_vector_cache: dict[bytes, object] = {}

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

    def pull_tx_buffer(self) -> tuple[object, int]:
        with self.lock:
            pcmu = self.session.pull_tx_frame(self._now_ns())
        # The stack encodes the conference-bridge PCM back to G.711 for the
        # wire; mu-law is a bijection on its own codebook, so handing it the
        # expansion of our PCMU frame reproduces the exact wire bytes.
        cached = self._tx_vector_cache.get(pcmu)
        if cached is None:
            pcm = decode_pcmu_to_pcm16_8k(pcmu)
            vector = pj.ByteVector()
            for byte in pcm:
                vector.append(byte)
            cached = self._tx_vector_cache[pcmu] = vector
        return cached, PCM16_8K_FRAME_BYTES

    def push_rx_pcm16(self, pcm16: bytes) -> None:
        if len(pcm16) != PCM16_8K_FRAME_BYTES:
            # Partial conference frames are stack artifacts, not media.
            return
        with self.lock:
            self._rx_sequence += 1
            self.session.handle_rx_frame(
                RxFrame(
                    pcmu=fast_encode_pcm16_to_pcmu(pcm16),
                    rtp_sequence=self._rx_sequence,
                    rtp_timestamp=self._rx_sequence * 160,
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
    # Fixed-depth jitter buffer: adaptivity trades timing determinism away
    # and unbounds the loss-counter-to-playout skew the void padding covers.
    ep_cfg.medConfig.jbInit = JITTER_BUFFER_MS
    ep_cfg.medConfig.jbMinPre = JITTER_BUFFER_MS
    ep_cfg.medConfig.jbMaxPre = JITTER_BUFFER_MS
    ep_cfg.medConfig.jbMax = JITTER_BUFFER_MS
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
    # Process-independent call bound (spec §4): if this process dies mid-call,
    # the unrefreshed session timer clears the dialog at the far end.
    acc_cfg.callConfig.timerUse = pj.PJSUA_SIP_TIMER_ALWAYS
    acc_cfg.callConfig.timerSessExpiresSec = 90
    acc_cfg.callConfig.timerMinSESec = 90
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
    answered_seen = False
    delivery: dict[str, object] = {}
    # Nothing is consumed before the first post-answer loss verdict: arm the
    # watermark at the pre-dial instant, ahead of any rx frame.
    with bridge.lock:
        session.set_scan_watermark(time.monotonic_ns())
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
            snapshot = None
            with bridge.lock:
                snapshot = session.pending_echo_check()
            if snapshot is not None:
                # Heavy correlation runs here, off the media path and outside
                # the lock, so the stack's frame clock is never starved.
                from bench.acoustic_probe import match_fixture_reference

                match = match_fixture_reference(
                    snapshot,
                    session.config.fixture,
                    maximum_alignment_ms=session.config.echo_search_ms,
                )
                with bridge.lock:
                    session.apply_echo_verdict(match, time.monotonic_ns())
            if bridge.answered and not answered_seen:
                # Interval accounting starts at answer: pre-answer counter
                # noise must not void (and instantly overrun) the timeline.
                answered_seen = True
                last_stats_ns = now_ns
                try:
                    known_loss = int(call.getStreamStat(0).rtcp.rxStat.loss)
                except pj.Error:
                    known_loss = 0
            if (
                answered_seen
                and now_ns - last_stats_ns >= STATS_POLL_NS
                and call.isActive()
            ):
                try:
                    stat = call.getStreamStat(0)
                    # The counter is pjmedia's locally computed rx loss,
                    # updated at packet arrival; clamp monotonically because
                    # late reordered arrivals can revise it downward.
                    loss = max(known_loss, int(stat.rtcp.rxStat.loss))
                    with bridge.lock:
                        if loss > known_loss:
                            session.report_rx_loss(
                                loss - known_loss,
                                last_stats_ns,
                                now_ns + VOID_END_PAD_NS,
                            )
                        # Windows are certified only once their interval's
                        # loss verdict is in; the pad keeps the not-yet-
                        # played concealment ahead of the watermark.
                        session.set_scan_watermark(now_ns - VOID_END_PAD_NS)
                    known_loss = loss
                    # Only advance on success: a failed poll leaves its span
                    # to be verdicted (and if lossy, voided) by the next one.
                    last_stats_ns = now_ns
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
                    "jitter_buffer_ms": JITTER_BUFFER_MS,
                    "void_end_pad_ms": VOID_END_PAD_NS // 1_000_000,
                    "session_backstops": {
                        "sip_session_timer_s": 90,
                        "harness_call_cap_s": 60,
                    },
                },
            )
        # No libDestroy: pjsua2 teardown aborted both live attempts (a
        # destructor-order assert, then an unregistered-thread assert from
        # the bounded destroyer). Artifacts are flushed, the BYE is out, and
        # the CLI hard-exits immediately after this returns, so the OS
        # reclaims the endpoint; SIP session timers bound the far end.
