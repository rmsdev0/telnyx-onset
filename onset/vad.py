"""Local WebRTC voice-activity detection for frame-level barge-in.

The barge-in trigger is acoustic onset on the inbound caller frames, not a
transcript: it fires within a couple of frames of the caller starting to speak,
with no network round trip. That is what makes interruption perceptibly faster
than a server-side detect-then-stop (the caller is heard locally and the
outbound queue is flushed in one socket frame). webrtcvad is stateless GMM
scoring with no ONNX, so is_speech runs inline per frame.

This detector emits only onset and re-arms after trailing silence. Turn-taking
(turn end and content) is driven entirely by the STT socket's speech_final, so
there is no VAD/STT race over when a turn ends.
"""

from __future__ import annotations

import enum

import structlog
import webrtcvad

log = structlog.get_logger()


class _State(enum.Enum):
    IDLE = "idle"
    SPEAKING = "speaking"


class VadDetector:
    """Detects the caller's speech onset from inbound PCM16 frames.

    Buffers audio into exact webrtcvad frames (10, 20, or 30 ms), runs a speech
    run counter, and returns True once on the frame where contiguous speech
    crosses the onset threshold. After the caller stops for the re-arm window it
    resets so the next utterance's onset fires again.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 2,
        speech_onset_ms: int = 120,
        silence_rearm_ms: int = 500,
    ) -> None:
        if frame_ms not in (10, 20, 30):
            raise ValueError("webrtcvad requires a 10, 20, or 30 ms frame")
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self._aggressiveness = aggressiveness
        self._speech_onset_ms = speech_onset_ms
        self._silence_rearm_ms = silence_rearm_ms
        self._vad = webrtcvad.Vad(aggressiveness)
        self._buffer = bytearray()
        self._state = _State.IDLE
        self._speech_run_ms = 0
        self._silence_run_ms = 0

    def process(self, pcm16: bytes) -> bool:
        """Feed inbound PCM16; return True once at speech onset.

        Returns True on at most one frame per utterance (the onset frame); every
        other frame returns False.
        """
        onset = False
        self._buffer.extend(pcm16)
        while len(self._buffer) >= self._frame_bytes:
            frame = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            if self._process_frame(frame):
                onset = True
        return onset

    def _process_frame(self, frame: bytes) -> bool:
        speech = bool(self._vad.is_speech(frame, self._sample_rate))
        if self._state is _State.IDLE:
            self._speech_run_ms = self._speech_run_ms + self._frame_ms if speech else 0
            if self._speech_run_ms >= self._speech_onset_ms:
                self._state = _State.SPEAKING
                self._silence_run_ms = 0
                log.debug("vad.onset")
                return True
            return False
        # SPEAKING: wait for trailing silence to re-arm for the next utterance.
        if speech:
            self._silence_run_ms = 0
        else:
            self._silence_run_ms += self._frame_ms
            if self._silence_run_ms >= self._silence_rearm_ms:
                self._rearm()
        return False

    def _rearm(self) -> None:
        self._state = _State.IDLE
        self._speech_run_ms = 0
        self._silence_run_ms = 0
        # webrtcvad has no reset method; rebuild the Vad to clear adapted state.
        self._vad = webrtcvad.Vad(self._aggressiveness)
