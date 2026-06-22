"""Structural interfaces the agent depends on.

The agent talks to its three sockets and the call through these Protocols, so the
real clients (media.py, stt.py, tts.py, telnyx.py) and the test fakes are
interchangeable and the agent never imports a concrete socket. This is also what
lets the whole loop be tested with no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from onset.types import LLMEvent, LLMMessage


class CallPort(Protocol):
    async def hangup(self) -> None: ...


class LLMPort(Protocol):
    def stream_response(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]: ...

    async def aclose(self) -> None: ...


class MediaPort(Protocol):
    def begin_utterance(self) -> int: ...

    async def send_audio_frame(self, epoch: int, frame: bytes) -> None: ...

    async def send_mark(self, epoch: int, name: str) -> None: ...

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


class SttPort(Protocol):
    async def connect(self) -> None: ...

    def feed(self, pcm16: bytes) -> None: ...

    async def aclose(self) -> None: ...


class TtsPort(Protocol):
    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...


class VadPort(Protocol):
    def process(self, pcm16: bytes) -> bool: ...
