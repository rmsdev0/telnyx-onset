"""Unit tests for transcript-to-turn assembly."""

from __future__ import annotations

from onset.turn_manager import TurnManager
from onset.types import STTEvent, STTEventType


def test_interim_while_listening_yields_no_turn() -> None:
    tm = TurnManager()
    # An interim with no buffered final must not start a turn.
    interim = STTEvent(STTEventType.TRANSCRIPT_INTERIM, "half a th")
    assert tm.handle_event(interim) is None
    # And an utterance-end with an empty buffer yields nothing.
    assert tm.handle_event(STTEvent(STTEventType.UTTERANCE_END)) is None


def test_final_then_utterance_end_yields_turn() -> None:
    tm = TurnManager()
    final = STTEvent(STTEventType.TRANSCRIPT_FINAL, "book a table")
    assert tm.handle_event(final) is None
    assert tm.handle_event(STTEvent(STTEventType.UTTERANCE_END)) == "book a table"


def test_set_listening_discards_partial_buffer() -> None:
    tm = TurnManager()
    tm.handle_event(STTEvent(STTEventType.TRANSCRIPT_FINAL, "stray words"))
    tm.set_listening()
    # The discarded final must not surface on the next utterance-end.
    assert tm.handle_event(STTEvent(STTEventType.UTTERANCE_END)) is None
