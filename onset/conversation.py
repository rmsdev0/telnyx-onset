"""Conversation state and per-call context for the voice agent.

Lifted from voice-agent-lite. Tracks the in-memory message history for a
single call, including tool calls and tool results, and stores assistant
turns cut short by barge-in truncated to what the caller actually heard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from onset.types import LLMMessage, ToolCallRequest

log = structlog.get_logger()


@dataclass
class CallContext:
    """Per-call mutable state that tools and prompts can read and write.

    Provides a place for tools to store collected information (slots),
    and for flow-controlled agents to track the current conversation node.
    """

    call_sid: str = ""
    from_number: str = ""
    start_time: float = field(default_factory=time.time)
    slots: dict[str, Any] = field(default_factory=dict)
    current_node: str | None = None


INTERRUPTED_MARKER = "[interrupted]"


class Conversation:
    """In-memory conversation history for a single call.

    Tracks messages in the format expected by LLM APIs, including
    tool call and tool result messages. Assistant turns cut short by
    caller barge-in are stored truncated to what was actually spoken,
    with an explicit marker so the LLM knows the caller did not hear
    the rest.
    """

    def __init__(self, max_turns: int = 20) -> None:
        self._messages: list[LLMMessage] = []
        self._max_turns = max_turns

    def add_user_turn(self, text: str) -> None:
        self._messages.append(LLMMessage(role="user", content=text))
        self._trim()

    def add_assistant_turn(
        self,
        text: str,
        interrupted: bool = False,
        tool_calls: list[ToolCallRequest] | None = None,
    ) -> None:
        content = text
        if interrupted:
            content = f"{text.rstrip()} {INTERRUPTED_MARKER}".strip()
            log.info("conversation.turn_interrupted", text=text[:80])
        self._messages.append(
            LLMMessage(role="assistant", content=content, tool_calls=tool_calls)
        )
        self._trim()

    def add_tool_result(self, tool_call_id: str, result: str) -> None:
        self._messages.append(
            LLMMessage(role="tool", content=result, tool_call_id=tool_call_id)
        )

    @property
    def messages(self) -> list[LLMMessage]:
        return list(self._messages)

    def to_log_dict(self) -> list[dict[str, Any]]:
        """Serialize for structured logging on call end."""
        entries = []
        for m in self._messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "name": tc.name} for tc in m.tool_calls
                ]
            entries.append(entry)
        return entries

    def _trim(self) -> None:
        """Keep conversation within max_turns (counted as user+assistant pairs)."""
        # Count user messages as a proxy for turns
        user_count = sum(1 for m in self._messages if m.role == "user")
        while user_count > self._max_turns and len(self._messages) > 2:
            self._messages.pop(0)
            user_count = sum(1 for m in self._messages if m.role == "user")
