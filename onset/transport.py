"""The minimal WebSocket surface the media stream depends on.

A Protocol, not the concrete FastAPI WebSocket, so MediaStream can be driven by
a fake socket in tests with no network. FastAPI's WebSocket satisfies it
structurally.
"""

from __future__ import annotations

from typing import Protocol


class WebSocketLike(Protocol):
    async def send_text(self, data: str) -> None: ...
