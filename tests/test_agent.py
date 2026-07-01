"""End-to-end agent loop tests using fakes for the three sockets and the LLM."""

from __future__ import annotations

import asyncio
import zlib
from collections import deque
from typing import TYPE_CHECKING, Any

import pytest

from onset.prompts import RESTAURANT_CONFIG
from onset.settings import Settings
from tests.conftest import (
    Harness,
    make_harness,
    submit_turn,
    text_round,
    tool_round,
    until,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from onset.types import LLMEvent, LLMMessage


class RaisingLLM:
    """An LLM whose stream always fails, to exercise the fallback path."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_response(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        raise RuntimeError("llm boom")
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        pass


class GatedLLM:
    """Replays rounds but blocks the first call, to exercise supersede-and-reap."""

    def __init__(self, rounds: Iterable[list[LLMEvent]]) -> None:
        self._rounds: deque[list[LLMEvent]] = deque(rounds)
        self.calls = 0
        self.gate = asyncio.Event()

    async def stream_response(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        events = self._rounds.popleft() if self._rounds else []
        if self.calls == 1:
            await self.gate.wait()
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_full_reservation_flow(settings: Settings) -> None:
    h = make_harness(
        settings,
        rounds=[
            tool_round(
                "c1",
                "check_availability",
                {"date": "Friday", "time": "7 PM", "party_size": 4},
            ),
            text_round("Great. What name should I put the table under?"),
            tool_round("c2", "make_reservation", {"name": "Alex"}),
            text_round("You're all set, Alex. See you Friday. Goodbye!"),
        ],
    )
    h.agent.start()

    # Greeting synthesizes first and completes before the caller speaks.
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)
    assert h.stt.connected
    assert h.tts.texts[0] == RESTAURANT_CONFIG.greeting
    assert h.media.frames  # audio was injected for the greeting
    assert h.agent._context.current_node == "booking"

    submit_turn(h.agent, "Book a table for four this Friday at 7 PM.")
    await until(lambda: h.agent._context.current_node == "confirm")
    await until(lambda: any("name" in t.lower() for t in h.tts.texts))
    assert h.agent._context.slots == {"date": "Friday", "time": "7 PM", "party_size": 4}

    submit_turn(h.agent, "The name is Alex.")
    await until(lambda: h.agent._context.current_node == "farewell")
    await until(lambda: h.call.hung_up)

    expected = f"GF-{zlib.crc32(b'Alex') % 10000:04d}"
    transcript = " ".join(m["content"] for m in h.agent._conversation.to_log_dict())
    assert expected in transcript

    # Telnyx would now post the media stop; simulate it to end the loop.
    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)

    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None
    assert h.stt.closed
    assert h.media.closed


@pytest.mark.asyncio
async def test_prebuffer_preserves_all_frames() -> None:
    # Prebuffer smaller than the reply so both the prebuffer collection and the
    # streaming remainder run; every synthesized frame must still reach the media
    # queue exactly once (no drop or duplicate across the split).
    settings = Settings(
        telnyx_api_key="test-key",
        telnyx_public_key="test-pub",
        half_duplex=False,
        tts_prebuffer_ms=20,  # 1 frame; FakeTts yields 2, so both loops run
    )
    h = make_harness(settings)
    assert h.agent._prebuffer_frames == 1
    h.agent.start()

    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)
    assert len(h.media.frames) == 2  # both greeting frames injected

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_barge_in_flushes_outbound_while_speaking(settings: Settings) -> None:
    # The greeting does not auto-complete, so the agent stays speaking and the
    # caller can interrupt it.
    h = make_harness(settings, auto_complete=False)
    h.agent.start()

    await until(lambda: h.agent._barge_in.agent_is_speaking)
    assert h.tts.texts[0] == RESTAURANT_CONFIG.greeting

    # An interim transcript while speaking triggers barge-in: the outbound queue
    # is flushed (clear) and the agent returns to listening.
    h.agent.submit_transcript("actually wait", is_final=False)
    await until(lambda: h.media.clears > 0)
    await until(lambda: not h.agent._barge_in.agent_is_speaking)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_vad_onset_triggers_barge_in(settings: Settings) -> None:
    # The local VAD onset is the frame-level barge-in trigger: feeding audio that
    # the VAD scores as speech onset, while speaking, flushes the outbound queue.
    h = make_harness(settings, auto_complete=False)
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)

    h.vad.fire = True
    h.agent.handle_audio(b"\x00" * 640)
    assert h.stt.fed  # inbound audio is also piped to STT
    await until(lambda: h.media.clears > 0)
    await until(lambda: not h.agent._barge_in.agent_is_speaking)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_no_barge_in_when_idle(settings: Settings) -> None:
    # An onset while the agent is NOT speaking must not flush anything.
    h = make_harness(settings)
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    h.vad.fire = True
    h.agent.handle_audio(b"\x00" * 640)
    await asyncio.sleep(0.05)
    assert h.media.clears == 0

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)


@pytest.mark.asyncio
async def test_half_duplex_feeds_silence_to_stt_while_speaking() -> None:
    # With half-duplex on, the caller's frame is replaced with silence while the
    # agent speaks: the echo never reaches STT, the socket stays warm (no idle
    # reconnect), and no barge-in fires.
    hd_settings = Settings(
        telnyx_api_key="k",
        telnyx_public_key="p",
        max_tokens_per_call=0,
        half_duplex=True,
        listen_guard_ms=10,
    )
    h = make_harness(hd_settings, auto_complete=False)
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)

    h.vad.fire = True
    h.agent.handle_audio(b"\xaa" * 640)  # a non-silent (echo) frame
    await asyncio.sleep(0.02)
    assert h.stt.fed == [b"\x00" * 640]  # replaced with silence, not the echo
    assert h.media.clears == 0  # no barge-in
    assert h.agent._barge_in.agent_is_speaking  # still speaking, not interrupted

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)


@pytest.mark.asyncio
async def test_interrupted_greeting_is_not_recorded(settings: Settings) -> None:
    h = make_harness(settings, auto_complete=False)
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)

    h.agent.submit_transcript("stop", is_final=False)
    await until(lambda: not h.agent._barge_in.agent_is_speaking)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)

    roles = [m["role"] for m in h.agent._conversation.to_log_dict()]
    assert "assistant" not in roles


@pytest.mark.asyncio
async def test_clean_shutdown_no_pending_tasks(settings: Settings) -> None:
    h = make_harness(settings)
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)

    assert h.agent._response_task is None or h.agent._response_task.done()
    assert h.stt.closed
    assert h.media.closed


@pytest.mark.asyncio
async def test_speak_completes_only_on_matching_generation(settings: Settings) -> None:
    h = make_harness(settings, auto_complete=False)
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)
    live_gen = h.agent._current_speak_gen

    # A completion for a different generation must not release the live speak.
    h.agent.submit_speak_ended(live_gen + 1)
    await asyncio.sleep(0.02)
    assert h.agent._barge_in.agent_is_speaking

    h.agent.submit_speak_ended(live_gen)
    await until(lambda: not h.agent._barge_in.agent_is_speaking)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_token_budget_announced_once_then_turns_skipped() -> None:
    budget_settings = Settings(
        telnyx_api_key="k", telnyx_public_key="p", max_tokens_per_call=1
    )
    from tests.conftest import FakeLLM

    llm = FakeLLM([text_round("unused")])
    h = make_harness(budget_settings, llm=llm)
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    submit_turn(h.agent, "I want a table.")
    await until(lambda: RESTAURANT_CONFIG.budget_exceeded_message in h.tts.texts)
    assert h.agent._budget_announced
    spoken_after_turn1 = len(h.tts.texts)

    submit_turn(h.agent, "Hello?")
    await asyncio.sleep(0.05)
    assert len(h.tts.texts) == spoken_after_turn1
    assert llm.calls == 0

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)


@pytest.mark.asyncio
async def test_tool_round_cap_falls_back(settings: Settings) -> None:
    args = {"date": "Mon", "time": "7", "party_size": 2}
    rounds = [
        tool_round(f"c{i}", "check_availability", args)
        for i in range(RESTAURANT_CONFIG.max_tool_rounds + 1)
    ]
    h = make_harness(settings, rounds=rounds)
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    submit_turn(h.agent, "Book something.")
    await until(lambda: RESTAURANT_CONFIG.fallback_message in h.tts.texts)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_llm_failure_speaks_fallback_without_crashing(settings: Settings) -> None:
    h = make_harness(settings, llm=RaisingLLM())
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    submit_turn(h.agent, "Anything.")
    await until(lambda: RESTAURANT_CONFIG.fallback_message in h.tts.texts)
    await until(lambda: not h.agent._barge_in.agent_is_speaking)
    assert h.agent.run_task is not None
    assert not h.agent.run_task.done()

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_run_loop_fault_hangs_up_and_tears_down(settings: Settings) -> None:
    h = make_harness(settings, rounds=[text_round("hi")])
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    def boom(_event: object) -> None:
        raise RuntimeError("loop boom")

    h.agent._turn_manager.handle_event = boom  # type: ignore[assignment]
    submit_turn(h.agent, "trigger")

    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None
    assert h.call.hung_up  # degraded path hangs up the live call
    assert h.stt.closed
    assert h.agent._closed


@pytest.mark.asyncio
async def test_stt_failure_winds_down_and_hangs_up(settings: Settings) -> None:
    # A socket reporting unrecoverable failure mid-call degrades gracefully: the
    # agent winds down and hangs up rather than leaving the caller in dead air.
    h = make_harness(settings)
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    h.agent._on_socket_error()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.call.hung_up
    assert h.stt.closed
    assert h.media.closed


@pytest.mark.asyncio
async def test_new_turn_supersedes_in_flight_response(settings: Settings) -> None:
    gated = GatedLLM([text_round("FIRST"), text_round("SECOND")])
    h = make_harness(settings, llm=gated)
    h.agent.start()
    await until(lambda: bool(h.tts.texts) and not h.agent._barge_in.agent_is_speaking)

    submit_turn(h.agent, "first request")
    await until(lambda: gated.calls == 1)
    assert not h.agent._barge_in.agent_is_speaking  # PROCESSING, not speaking

    submit_turn(h.agent, "second request")
    await until(lambda: "SECOND" in h.tts.texts)
    assert "FIRST" not in h.tts.texts

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_barge_in_then_next_turn_is_handled(settings: Settings) -> None:
    # Barge-in must not wedge the agent: a real turn after the interruption is
    # handled normally.
    h = make_harness(
        settings, rounds=[text_round("Sure, one moment.")], auto_complete=False
    )
    h.agent.start()
    await until(lambda: h.agent._barge_in.agent_is_speaking)

    h.agent.submit_transcript("wait", is_final=False)
    await until(lambda: not h.agent._barge_in.agent_is_speaking)

    # Let the next turn's speak complete so we can observe it finish.
    h.media._auto = True
    submit_turn(h.agent, "Tell me the specials.")
    await until(lambda: "Sure, one moment." in h.tts.texts)

    h.agent.submit_hangup()
    await asyncio.wait_for(_run(h), timeout=2.0)
    assert h.agent.run_task is not None
    assert h.agent.run_task.exception() is None


def _run(h: Harness) -> asyncio.Task[None]:
    """The agent's run task (asserts it was started)."""
    assert h.agent.run_task is not None
    return h.agent.run_task
