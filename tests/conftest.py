"""Shared test fakes and helpers for the media-stream agent.

No network: the three sockets and the call are replaced by in-memory fakes that
implement the same Protocols the agent depends on. FakeMedia records injected
frames, clears, and marks, and by default echoes each mark back as a speak
completion the way Telnyx does once playback drains. FakeLLM replays scripted
rounds of LLM events.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from onset.agent import VoiceAgent
from onset.prompts import RESTAURANT_CONFIG
from onset.settings import Settings
from onset.types import LLMEvent, LLMEventType, LLMMessage, ToolCallRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable

    from onset.ports import LLMPort


@pytest.fixture
def settings() -> Settings:
    # half_duplex off so the full-duplex barge-in path is exercised; the
    # half-duplex gate has its own dedicated test.
    return Settings(
        telnyx_api_key="test-key",
        telnyx_public_key="test-pub",
        max_tokens_per_call=0,
        half_duplex=False,
    )


class FakeCall:
    """The Call Control surface the agent needs: just hangup."""

    def __init__(self) -> None:
        self.id = "cc-test"
        self.hung_up = False

    async def hangup(self) -> None:
        self.hung_up = True


class FakeMedia:
    """Records outbound injection; echoes marks as speak completions by default."""

    def __init__(self, *, auto_complete: bool = True) -> None:
        self.frames: list[bytes] = []
        self.marks: list[str] = []
        self.clears = 0
        self.closed = False
        self._epoch = 0
        self._auto = auto_complete
        self.agent: VoiceAgent | None = None  # set by the test for mark echo

    def begin_utterance(self) -> int:
        return self._epoch

    async def send_audio_frame(self, epoch: int, frame: bytes) -> None:
        if epoch == self._epoch:
            self.frames.append(frame)

    async def send_mark(self, epoch: int, name: str) -> None:
        if epoch != self._epoch:
            return
        self.marks.append(name)
        if self._auto and self.agent is not None and name.startswith("speak:"):
            self.agent.submit_speak_ended(int(name.split(":", 1)[1]))

    async def flush(self) -> None:
        # Mirror the real flush: bump the epoch so in-flight frames are dropped.
        self._epoch += 1
        self.clears += 1

    async def aclose(self) -> None:
        self.closed = True


class FakeStt:
    """Records fed audio and connect/close lifecycle; drives no transcripts."""

    def __init__(self) -> None:
        self.fed: list[bytes] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    def feed(self, pcm16: bytes) -> None:
        self.fed.append(pcm16)

    async def aclose(self) -> None:
        self.closed = True


class FakeTts:
    """Yields a few silence frames per call; can block to simulate in-flight TTS."""

    def __init__(self, *, frames: int = 2, block: bool = False) -> None:
        self._frames = frames
        self._block = block
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.texts.append(text)
        for _ in range(self._frames):
            yield b"\x00" * 640
        if self._block:
            await asyncio.Event().wait()  # never returns: TTS still in flight


class FakeVad:
    """Fires an onset on the next frame when .fire is set."""

    def __init__(self) -> None:
        self.fire = False

    def process(self, pcm16: bytes) -> bool:
        if self.fire:
            self.fire = False
            return True
        return False


class FakeLLM:
    """Replays scripted rounds of LLM events, one round per stream call."""

    def __init__(self, rounds: Iterable[list[LLMEvent]]) -> None:
        self._rounds: deque[list[LLMEvent]] = deque(rounds)
        self.calls = 0

    async def stream_response(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        events = self._rounds.popleft() if self._rounds else []
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


@dataclass
class Harness:
    agent: VoiceAgent
    call: FakeCall
    media: FakeMedia
    stt: FakeStt
    tts: FakeTts
    vad: FakeVad


def make_harness(
    settings: Settings,
    *,
    rounds: Iterable[list[LLMEvent]] = (),
    auto_complete: bool = True,
    llm: LLMPort | None = None,
) -> Harness:
    """Build an agent wired to fresh fakes, with FakeLLM unless one is given."""
    call = FakeCall()
    media = FakeMedia(auto_complete=auto_complete)
    stt = FakeStt()
    tts = FakeTts()
    vad = FakeVad()
    agent = VoiceAgent(
        settings, call, media, RESTAURANT_CONFIG, stt=stt, tts=tts, vad=vad
    )
    agent._llm = llm if llm is not None else FakeLLM(rounds)
    agent.set_call_info(call.id, "+15551234567")
    media.agent = agent
    return Harness(agent, call, media, stt, tts, vad)


def submit_turn(agent: VoiceAgent, text: str) -> None:
    """Drive one complete user turn: a final transcript plus speech_final."""
    agent.submit_transcript(text, is_final=True, speech_final=True)


def text_round(text: str) -> list[LLMEvent]:
    return [LLMEvent(type=LLMEventType.TEXT_DELTA, text=text)]


def tool_round(call_id: str, name: str, args: dict[str, Any]) -> list[LLMEvent]:
    return [
        LLMEvent(
            type=LLMEventType.TOOL_CALL,
            tool_call=ToolCallRequest(
                id=call_id, name=name, arguments=json.dumps(args)
            ),
        )
    ]


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait until predicate() is truthy or fail after timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")
