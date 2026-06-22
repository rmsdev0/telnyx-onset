"""Barge-in detection: cancel agent speech when the caller interrupts.

Lifted from voice-agent-lite. The one carrier touchpoint (flushing playback)
is collapsed: instead of clearing a media-stream buffer, the handler calls a
stop coroutine that issues the Telnyx playback_stop call command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger()


class BargeInHandler:
    """Tracks whether the agent is speaking and handles interruption.

    When the caller starts speaking while the agent is still talking, barge-in
    stops the leg's playback and signals the pipeline to cancel the in-flight
    response task.
    """

    def __init__(self) -> None:
        self.agent_is_speaking = False
        self._barged_in = False

    @property
    def was_interrupted(self) -> bool:
        """True if the most recent agent turn was interrupted by barge-in."""
        result = self._barged_in
        self._barged_in = False
        return result

    async def handle_barge_in(
        self, stop_playback: Callable[[], Awaitable[None]]
    ) -> None:
        """Stop the leg's playback to interrupt the agent.

        A failed stop must not crash the call, so it is logged rather than
        raised: the response task is cancelled regardless.
        """
        if not self.agent_is_speaking:
            return

        log.info("barge_in.triggered")
        self._barged_in = True
        self.agent_is_speaking = False

        try:
            await stop_playback()
        except Exception as e:
            log.warning("barge_in.stop_failed", error=str(e))
