"""Bounded retry helper for streaming connections.

Lifted from voice-agent-lite. Retries are only attempted before a stream has
produced any output. Once a stream has yielded, errors propagate instead of
retrying, so a retried LLM request can never duplicate caller-facing audio or
re-bill work that already returned data.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

log = structlog.get_logger()


async def retry_stream[T](
    stream_factory: Callable[[], AsyncIterator[T]],
    *,
    max_retries: int = 2,
    backoff_s: float = 0.25,
    name: str = "stream",
) -> AsyncIterator[T]:
    """Iterate a stream, retrying transient failures with backoff.

    The factory is called to open a fresh stream on each attempt.
    A failure is retried (up to max_retries extra attempts, with
    exponential backoff) only if the current attempt has not yielded
    anything yet. Cancellation is never swallowed.
    """
    for attempt in range(max_retries + 1):
        yielded = False
        try:
            async for item in stream_factory():
                yielded = True
                yield item
        except Exception as e:
            if yielded or attempt >= max_retries:
                raise
            delay = backoff_s * (2**attempt)
            log.warning(
                "retry.stream_failed",
                name=name,
                attempt=attempt + 1,
                max_retries=max_retries,
                delay_s=delay,
                error=str(e),
            )
            await asyncio.sleep(delay)
        else:
            return
