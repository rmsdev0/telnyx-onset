"""Call volume and spend protection.

Lifted from voice-agent-lite. CallLimiter caps how many calls can be active at
once, so a traffic spike cannot fan out into unbounded provider spend.
TokenBudget caps estimated LLM token usage for a single call, so one runaway
conversation cannot burn through an API budget.

Both are configured via Settings (max_concurrent_calls and
max_tokens_per_call); a value of 0 disables the corresponding limit.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()

# Rough estimate used because streaming responses do not report usage
CHARS_PER_TOKEN = 4


class CallLimiter:
    """Tracks active calls and enforces a concurrency cap.

    Single event loop only: counters are plain ints with no locking.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._max = max_concurrent
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def at_capacity(self) -> bool:
        return self._max > 0 and self._active >= self._max

    def try_acquire(self) -> bool:
        """Reserve a call slot. Returns False if at capacity."""
        if self.at_capacity:
            log.warning(
                "limits.call_rejected", active=self._active, max_concurrent=self._max
            )
            return False
        self._active += 1
        return True

    def release(self) -> None:
        self._active = max(0, self._active - 1)


class TokenBudget:
    """Approximate per-call LLM token budget.

    Usage is estimated from character counts (roughly 4 characters per
    token) across prompt input and streamed output, since streaming
    APIs do not return usage data.
    """

    def __init__(self, max_tokens: int) -> None:
        self._max = max_tokens
        self._chars = 0

    @property
    def enabled(self) -> bool:
        return self._max > 0

    @property
    def used(self) -> int:
        """Estimated tokens consumed so far."""
        return self._chars // CHARS_PER_TOKEN

    @property
    def exhausted(self) -> bool:
        return self.enabled and self.used >= self._max

    def record_text(self, text: str) -> None:
        self._chars += len(text)
