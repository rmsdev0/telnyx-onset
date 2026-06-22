"""Shared event and message types.

Lifted from the voice-agent-lite provider contract. These dataclasses and
enums are the vocabulary the orchestration core speaks: STT events drive the
turn manager and barge-in, LLM events carry text and tool calls, and the
message types model the conversation history sent to the LLM.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class STTEventType(enum.Enum):
    TRANSCRIPT_INTERIM = "transcript_interim"
    TRANSCRIPT_FINAL = "transcript_final"
    UTTERANCE_END = "utterance_end"
    # Acoustic onset from the local VAD: the barge-in trigger. Carries no
    # transcript and is ignored by the turn manager (turns come from STT).
    SPEECH_STARTED = "speech_started"


@dataclass(frozen=True, slots=True)
class STTEvent:
    """A single event in the agent loop.

    Either a transcript from the STT socket (interim or final, with the STT's
    end-of-utterance surfaced as UTTERANCE_END) or a SPEECH_STARTED onset from
    the local VAD.
    """

    type: STTEventType
    transcript: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A tool call requested by the LLM."""

    id: str
    name: str
    arguments: str  # JSON string


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """A message in the conversation history."""

    role: str  # "system", "user", "assistant", "tool"
    content: str = ""
    tool_calls: list[ToolCallRequest] | None = None
    tool_call_id: str | None = None


class LLMEventType(enum.Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"


@dataclass(frozen=True, slots=True)
class LLMEvent:
    """An event yielded by the LLM streaming response."""

    type: LLMEventType
    text: str = ""
    tool_call: ToolCallRequest | None = None
