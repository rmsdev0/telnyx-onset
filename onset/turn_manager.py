"""Turn assembly from transcription events.

Lifted from voice-agent-lite. Accumulates final transcripts and emits a
complete user turn when an UtteranceEnd arrives with a non-empty buffer. In
this build each Telnyx final transcription is followed by a synthesized
UtteranceEnd, so a final transcript becomes a complete turn.
"""

from __future__ import annotations

import time

import structlog

from onset.types import STTEvent, STTEventType

log = structlog.get_logger()


class TurnManager:
    """Accumulates STT events and detects when the user has finished a turn."""

    def __init__(self) -> None:
        self._transcript_buffer: list[str] = []
        self._turn_start_time: float | None = None

    def handle_event(self, event: STTEvent) -> str | None:
        """Process an STT event and return a complete user turn if detected.

        Returns the full user utterance string when a turn is complete,
        or None if still accumulating.
        """
        if event.type == STTEventType.TRANSCRIPT_FINAL:
            self._start_turn_clock()
            self._transcript_buffer.append(event.transcript)
            log.debug("turn.transcript_final", transcript=event.transcript)
            return None

        if event.type == STTEventType.TRANSCRIPT_INTERIM:
            self._start_turn_clock()
            log.debug("turn.transcript_interim", transcript=event.transcript)
            return None

        if event.type == STTEventType.UTTERANCE_END:
            if not self._transcript_buffer:
                return None

            utterance = " ".join(self._transcript_buffer)
            duration = (
                time.monotonic() - self._turn_start_time
                if self._turn_start_time
                else 0.0
            )

            log.info(
                "turn.complete",
                utterance=utterance,
                duration_s=round(duration, 3),
                num_finals=len(self._transcript_buffer),
            )

            self._transcript_buffer.clear()
            self._turn_start_time = None
            return utterance

        return None

    def _start_turn_clock(self) -> None:
        """Mark the turn's start at the first sign of speech."""
        if self._turn_start_time is None:
            self._turn_start_time = time.monotonic()

    def set_listening(self) -> None:
        """Reset for the next turn, discarding any partial transcript buffer."""
        self._transcript_buffer.clear()
        self._turn_start_time = None
