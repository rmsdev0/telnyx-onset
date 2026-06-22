"""Agent orchestration for a single Telnyx media-stream call.

The orchestration core (turn manager, conversation, tool loop with flow nodes,
token budget, interrupted-turn rollback, the LLM client) is lifted intact. Only
the I/O seams are wired to the three media-stream sockets:

- Audio in: the server's media read loop calls handle_audio() with each inbound
  PCM16 frame; the agent feeds it to the STT socket and to the local VAD.
- Transcripts: the STT socket calls submit_transcript(); a speech_final closes
  the user's turn.
- Barge-in: a VAD onset (or any transcript) while the agent is speaking flushes
  the outbound media queue with a clear event, cancels the in-flight response,
  and returns to listening. This is the frame-level interrupt.
- Audio out: a finished response is synthesized on the TTS socket, decoded, and
  paced into the call as media frames; a mark fence echoed by Telnyx signals
  playback complete.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from onset.barge_in import BargeInHandler
from onset.conversation import CallContext, Conversation
from onset.limits import TokenBudget
from onset.prompts import build_system_prompt
from onset.retry import retry_stream
from onset.stt import SttClient
from onset.telnyx import TelnyxLLM
from onset.tts import TtsClient
from onset.turn_manager import TurnManager
from onset.types import (
    LLMEvent,
    LLMEventType,
    LLMMessage,
    STTEvent,
    STTEventType,
    ToolCallRequest,
)
from onset.vad import VadDetector

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from onset.ports import CallPort, LLMPort, MediaPort, SttPort, TtsPort, VadPort
    from onset.prompts import AgentConfig, FlowNode
    from onset.settings import Settings

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SpeakEnded:
    """Signals that a spoken turn finished playing (a mark fence echoed back).

    generation correlates with the speak that produced it (via the mark name).
    None means the correlation id could not be read; it never matches a live
    speak, so such a completion is ignored and the speak timeout takes over.
    """

    generation: int | None


# Sentinel pushed onto the queue to end the run loop (caller or agent hangup).
_HANGUP = object()

# Queue items: an STTEvent, a SpeakEnded, or the _HANGUP sentinel.
_QueueItem = object


class VoiceAgent:
    """Orchestrates a single voice call across the three media-stream sockets."""

    def __init__(
        self,
        settings: Settings,
        call: CallPort,
        media: MediaPort,
        agent_config: AgentConfig,
        *,
        stt: SttPort | None = None,
        tts: TtsPort | None = None,
        vad: VadPort | None = None,
    ) -> None:
        self._settings = settings
        self._call = call
        self._media = media
        self._config = agent_config

        # Per-call state
        self._context = CallContext()
        self._conversation = Conversation()
        self._turn_manager = TurnManager()
        # Set when the conversation should end (terminal flow node); the call
        # hangs up after the final reply finishes playing.
        self._ending = False
        # Set when a socket failed unrecoverably mid-call: the run loop winds
        # down and hangs up rather than leaving the caller in dead air.
        self._degraded = False
        self._barge_in = BargeInHandler()

        self._llm: LLMPort = TelnyxLLM(settings)

        # Spend protection: estimated token budget for this call
        self._budget = TokenBudget(settings.max_tokens_per_call)
        self._budget_announced = False

        # Event source: handle_audio and the sockets push transcripts, speak
        # completions, VAD onsets, and the hangup sentinel here.
        self._events: asyncio.Queue[_QueueItem] = asyncio.Queue()

        # Tasks
        self._run_task: asyncio.Task[None] | None = None
        self._response_task: asyncio.Task[None] | None = None

        self._closed = False

        # speak completion tracking. Each speak gets a monotonically increasing
        # generation carried in the mark name, so a stale mark echo (for a speak
        # we already interrupted) cannot complete a later one.
        self._speak_gen = 0
        self._current_speak_gen = -1
        self._speak_done = asyncio.Event()

        # Half-duplex listening gate (see handle_audio): while speaking, plus a
        # guard tail, the caller's frame is replaced with silence so echo cannot
        # self-interrupt, while the STT socket stays warm (no idle reconnect).
        self._half_duplex = settings.half_duplex
        self._listen_guard_s = settings.listen_guard_ms / 1000.0
        self._mute_until = 0.0
        self._silence_frame = b"\x00" * settings.frame_bytes

        # The three sockets and the local VAD, injectable for tests.
        self._stt: SttPort = (
            stt
            if stt is not None
            else SttClient(
                settings,
                on_transcript=self.submit_transcript,
                on_error=self._on_socket_error,
            )
        )
        self._tts: TtsPort = tts if tts is not None else TtsClient(settings)
        self._vad: VadPort = (
            vad
            if vad is not None
            else VadDetector(
                sample_rate=settings.sample_rate,
                frame_ms=settings.frame_ms,
                aggressiveness=settings.vad_aggressiveness,
                speech_onset_ms=settings.vad_speech_onset_ms,
                silence_rearm_ms=settings.vad_silence_rearm_ms,
            )
        )

    # ── Setup and event submission ───────────────────────────────

    def set_call_info(self, call_sid: str, from_number: str) -> None:
        self._context.call_sid = call_sid
        self._context.from_number = from_number
        if self._config.initial_node:
            self._context.current_node = self._config.initial_node

    def start(self) -> asyncio.Task[None]:
        """Begin the run loop. Called once the media stream is live."""
        self._run_task = asyncio.create_task(self.run())
        return self._run_task

    @property
    def run_task(self) -> asyncio.Task[None] | None:
        return self._run_task

    def handle_audio(self, pcm16: bytes) -> None:
        """Route one inbound PCM16 frame to the STT socket and the local VAD.

        Called by the server's media read loop, once per ~20 ms inbound frame.
        Non-blocking: the STT feed is a bounded drop-oldest enqueue and the VAD
        is fast inline scoring, so the read loop is never stalled.

        In half-duplex mode the frame is dropped while the agent is speaking, and
        for a short guard after, so it never transcribes or barges in on its own
        voice through line or acoustic echo.
        """
        if self._closed:
            return
        if self._half_duplex:
            now = asyncio.get_running_loop().time()
            if self._barge_in.agent_is_speaking:
                self._mute_until = now + self._listen_guard_s
            if self._barge_in.agent_is_speaking or now < self._mute_until:
                # Replace the caller's frame (which carries our echo) with
                # silence: the agent never hears itself, and the STT socket keeps
                # receiving audio so it does not idle out and reconnect, which
                # would clip the start of the caller's next reply.
                self._stt.feed(self._silence_frame)
                return
        self._stt.feed(pcm16)
        if self._vad.process(pcm16):
            self._events.put_nowait(STTEvent(STTEventType.SPEECH_STARTED))

    def submit_transcript(
        self, transcript: str, is_final: bool, speech_final: bool = False
    ) -> None:
        """Push a Telnyx STT transcript as agent events.

        speech_final marks the end of the caller's utterance, so it is followed
        by an UtteranceEnd to close the turn; a plain final only accumulates.
        """
        if speech_final:
            if transcript:
                self._events.put_nowait(
                    STTEvent(STTEventType.TRANSCRIPT_FINAL, transcript)
                )
            self._events.put_nowait(STTEvent(STTEventType.UTTERANCE_END))
        elif is_final:
            self._events.put_nowait(STTEvent(STTEventType.TRANSCRIPT_FINAL, transcript))
        else:
            self._events.put_nowait(
                STTEvent(STTEventType.TRANSCRIPT_INTERIM, transcript)
            )

    def submit_speak_ended(self, generation: int | None) -> None:
        self._events.put_nowait(SpeakEnded(generation))

    def submit_hangup(self) -> None:
        self._events.put_nowait(_HANGUP)

    def _on_socket_error(self) -> None:
        """A socket failed unrecoverably: wind the call down and hang up."""
        log.error("agent.socket_failed")
        self._degraded = True
        self._events.put_nowait(_HANGUP)

    # ── Main loop ────────────────────────────────────────────────

    async def run(self) -> None:
        """Drive the call: open the STT socket, greet, then process events."""
        log.info("agent.call_started", config=self._config.name)
        try:
            try:
                await self._stt.connect()
            except Exception:
                # Without STT the caller cannot be heard. Do not leave them in
                # dead air: end the call once the greeting has played. _greet
                # hangs up when _ending.
                log.exception("agent.stt_connect_failed")
                self._ending = True

            if self._config.greeting:
                self._response_task = asyncio.create_task(self._greet())
            elif self._ending:
                with contextlib.suppress(Exception):
                    await self._call.hangup()
                return

            while True:
                item = await self._events.get()
                if item is _HANGUP:
                    log.info("agent.hangup_received")
                    break
                if isinstance(item, SpeakEnded):
                    self._on_speak_ended(item.generation)
                    continue

                assert isinstance(item, STTEvent)
                event = item

                # Once the conversation is ending, ignore further input so
                # background noise cannot start a new turn before the hangup.
                if self._ending:
                    continue

                # Barge-in: a VAD onset (fastest) or any transcript while the
                # agent is speaking. The stop action flushes the outbound media
                # queue and clears Telnyx's buffer at the frame level.
                is_barge_in_signal = event.type in (
                    STTEventType.SPEECH_STARTED,
                    STTEventType.TRANSCRIPT_INTERIM,
                    STTEventType.TRANSCRIPT_FINAL,
                )
                # In half-duplex the agent does not listen to itself, so a signal
                # arriving mid-speech is a stale late-arriving transcript, never a
                # live interruption; let the turn finish rather than flushing it.
                if (
                    is_barge_in_signal
                    and self._barge_in.agent_is_speaking
                    and not self._half_duplex
                ):
                    await self._barge_in.handle_barge_in(self._media.flush)
                    if self._response_task and not self._response_task.done():
                        self._response_task.cancel()
                    self._turn_manager.set_listening()
                    continue

                user_turn = self._turn_manager.handle_event(event)
                if user_turn:
                    # A newly completed turn supersedes any response still in
                    # flight. Cancel and reap it first so only one response runs
                    # at a time; overlapping responses would corrupt the shared
                    # speak-completion state and wedge agent_is_speaking on.
                    if self._response_task and not self._response_task.done():
                        log.info("agent.superseding_response")
                        self._response_task.cancel()
                        with contextlib.suppress(Exception):
                            await self._response_task
                    self._conversation.add_user_turn(user_turn)
                    self._response_task = asyncio.create_task(self._generate_response())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent.run_failed")
            self._degraded = True
        finally:
            if self._degraded:
                with contextlib.suppress(Exception):
                    await self._call.hangup()
            await self._teardown()

    async def _teardown(self) -> None:
        """Cancel the in-flight turn and close every socket cleanly."""
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._response_task
        with contextlib.suppress(Exception):
            await self._stt.aclose()
        with contextlib.suppress(Exception):
            await self._media.aclose()
        await self.aclose()
        log.info("agent.call_ended", conversation=self._conversation.to_log_dict())

    async def aclose(self) -> None:
        """Close the LLM client. Idempotent.

        The server calls this for agents that were registered but never started,
        whose run loop and _teardown never ran, so the client would otherwise
        leak.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._llm.aclose()

    def _on_speak_ended(self, generation: int | None) -> None:
        """Release the speak waiter only if this matches the live speak."""
        if generation == self._current_speak_gen:
            self._speak_done.set()

    # ── Response generation ──────────────────────────────────────

    def _build_tools_schemas(self) -> list[dict[str, Any]] | None:
        """Tools offered to the LLM for the current flow node."""
        if not self._config.tools:
            return None
        node = self._get_current_node()
        if node and node.tools is not None:
            schemas = list(self._config.tools.subset_schemas(node.tools))
        else:
            schemas = list(self._config.tools.get_schemas())
        return schemas or None

    async def _generate_response(self) -> None:
        """Run the LLM, the tool loop, then speak the reply for one turn."""
        t_start = time.monotonic()

        budget_message: str | None = None
        if self._budget.exhausted:
            if self._budget_announced:
                log.warning(
                    "agent.turn_skipped_budget_exhausted", used=self._budget.used
                )
                return
            self._budget_announced = True
            log.warning("agent.token_budget_exhausted", used=self._budget.used)
            budget_message = self._config.budget_exceeded_message

        system_prompt = build_system_prompt(
            self._config,
            self._context,
            self._context.current_node,
        )

        tools_schemas = self._build_tools_schemas()
        messages = self._conversation.messages

        full_text = ""
        # Tokens actually injected before any interruption (appended in _speak
        # only after a turn's frames are all enqueued).
        spoken_tokens: list[str] = []
        tool_rounds = 0
        try:
            if budget_message is not None:
                full_text = budget_message
                await self._speak([full_text], spoken_tokens)
                return

            # Charge the prompt and starting history to the budget once; the
            # streamed deltas, tool-call args, and tool results are each recorded
            # as they are produced, so the loop must NOT re-record the prompt or
            # accumulated history (which would over-count it once per tool round).
            self._budget.record_text(system_prompt)
            for m in messages:
                self._budget.record_text(m.content)

            while True:
                if self._budget.exhausted:
                    self._budget_announced = True
                    log.warning("agent.token_budget_exhausted", used=self._budget.used)
                    full_text = self._config.budget_exceeded_message
                    await self._speak([full_text], spoken_tokens)
                    break

                text_tokens: list[str] = []
                tool_calls: list[ToolCallRequest] = []
                t_first_token = None

                def open_llm_stream(
                    m: list[LLMMessage] = messages,
                    s: str = system_prompt,
                    t: list[dict[str, Any]] | None = tools_schemas,
                ) -> AsyncIterator[LLMEvent]:
                    return self._llm.stream_response(m, system=s, tools=t)

                async for event in retry_stream(
                    open_llm_stream,
                    max_retries=self._settings.stream_max_retries,
                    backoff_s=self._settings.stream_retry_backoff_s,
                    name="llm",
                ):
                    if event.type == LLMEventType.TEXT_DELTA:
                        if t_first_token is None:
                            t_first_token = time.monotonic()
                            ttft_ms = round((t_first_token - t_start) * 1000)
                            log.info("latency.llm_ttft", ms=ttft_ms)
                        text_tokens.append(event.text)
                        self._budget.record_text(event.text)
                    elif event.type == LLMEventType.TOOL_CALL and event.tool_call:
                        tool_calls.append(event.tool_call)
                        self._budget.record_text(event.tool_call.arguments)

                if tool_calls and self._config.tools:
                    tool_rounds += 1
                    if tool_rounds > self._config.max_tool_rounds:
                        log.warning(
                            "agent.tool_rounds_exceeded",
                            rounds=tool_rounds,
                            max_rounds=self._config.max_tool_rounds,
                        )
                        full_text = self._config.fallback_message
                        await self._speak([full_text], spoken_tokens)
                        break

                    self._conversation.add_assistant_turn("", tool_calls=tool_calls)

                    # Every tool_call id MUST get a matching tool result, or the
                    # next LLM request is malformed (the API rejects unmatched
                    # tool_call ids) and wedges the call. The finally fills any the
                    # loop did not reach, e.g. cancellation (supersede/teardown)
                    # mid-round, so history is never left inconsistent.
                    answered: set[str] = set()
                    try:
                        for tc in tool_calls:
                            result = await self._config.tools.execute(
                                tc.name,
                                tc.arguments,
                                call_context=self._context,
                            )
                            self._conversation.add_tool_result(tc.id, result)
                            self._budget.record_text(result)
                            answered.add(tc.id)
                            self._handle_transition(tc.name)
                    finally:
                        for tc in tool_calls:
                            if tc.id not in answered:
                                self._conversation.add_tool_result(
                                    tc.id, "(interrupted)"
                                )

                    system_prompt = build_system_prompt(
                        self._config,
                        self._context,
                        self._context.current_node,
                    )
                    tools_schemas = self._build_tools_schemas()
                    messages = self._conversation.messages
                    continue

                full_text = "".join(text_tokens)
                if full_text.strip():
                    await self._speak(text_tokens, spoken_tokens)

                break  # Done with this turn

        except asyncio.CancelledError:
            log.info("agent.response_cancelled")
        except Exception:
            log.exception("agent.response_failed")
            full_text = await self._speak_fallback(spoken_tokens)
        finally:
            interrupted = self._barge_in.was_interrupted
            if interrupted:
                spoken_text = "".join(spoken_tokens).strip()
                if spoken_text:
                    self._conversation.add_assistant_turn(spoken_text, interrupted=True)
            elif full_text:
                self._conversation.add_assistant_turn(full_text)
            self._barge_in.agent_is_speaking = False
            self._turn_manager.set_listening()

            total_ms = round((time.monotonic() - t_start) * 1000)
            log.info("latency.total", ms=total_ms, interrupted=interrupted)

            if self._ending:
                with contextlib.suppress(Exception):
                    await self._call.hangup()

    async def _greet(self) -> None:
        """Speak the configured greeting when the call connects."""
        spoken_tokens: list[str] = []
        try:
            await self._speak([self._config.greeting], spoken_tokens)
        except asyncio.CancelledError:
            log.info("agent.greeting_cancelled")
        finally:
            _ = self._barge_in.was_interrupted
            self._barge_in.agent_is_speaking = False
            self._turn_manager.set_listening()
            if self._ending:
                with contextlib.suppress(Exception):
                    await self._call.hangup()

    async def _speak(self, text_tokens: list[str], spoken_tokens: list[str]) -> None:
        """Synthesize text on the TTS socket and pace it into the call.

        spoken_tokens is appended only after all of this turn's frames are
        enqueued, so a turn interrupted mid-synthesis records nothing rather than
        claiming the caller heard the whole reply. Completion comes from the mark
        fence echoed by Telnyx once the audio drains; a length-scaled timeout is
        the safety net.
        """
        self._barge_in.agent_is_speaking = True

        text = "".join(text_tokens)
        if not text.strip():
            return

        self._speak_gen += 1
        generation = self._speak_gen
        self._current_speak_gen = generation
        self._speak_done = asyncio.Event()

        t_speak = time.monotonic()
        epoch = self._media.begin_utterance()
        frames = 0
        async for frame in self._tts.synthesize(text):
            if frames == 0:
                # Time to the first audio frame: TTS synthesis plus first decode.
                log.info(
                    "latency.first_audio",
                    ms=round((time.monotonic() - t_speak) * 1000),
                )
            await self._media.send_audio_frame(epoch, frame)
            frames += 1
        # All frames are now enqueued; record the turn as spoken. A barge-in that
        # cancels mid-synthesis lands before this, so spoken_tokens stays empty
        # and the interrupted turn is dropped rather than recorded as fully heard.
        spoken_tokens.extend(text_tokens)
        # Fence the end of this utterance; Telnyx echoes the mark once the audio
        # ahead of it has finished playing, which is our playback-complete signal.
        await self._media.send_mark(epoch, f"speak:{generation}")
        log.info(
            "speak.injected", generation=generation, frames=frames, chars=len(text)
        )

        timeout = max(15.0, len(text) / 10 + 10.0)
        try:
            await asyncio.wait_for(self._speak_done.wait(), timeout)
        except TimeoutError:
            log.warning("speak.timeout", generation=generation, chars=len(text))
        log.info("latency.speak_total", ms=round((time.monotonic() - t_speak) * 1000))

    async def _speak_fallback(self, spoken_tokens: list[str]) -> str:
        """Best-effort spoken apology after a pipeline failure."""
        try:
            await self._speak([self._config.fallback_message], spoken_tokens)
        except Exception:
            log.exception("agent.fallback_speech_failed")
        return "".join(spoken_tokens).strip()

    # ── Flow nodes ───────────────────────────────────────────────

    def _get_current_node(self) -> FlowNode | None:
        if (
            self._config.nodes
            and self._context.current_node
            and self._context.current_node in self._config.nodes
        ):
            return self._config.nodes[self._context.current_node]
        return None

    def _handle_transition(self, tool_name: str) -> None:
        """Transition to the next flow node if the tool triggers one."""
        node = self._get_current_node()
        if node and tool_name in node.transitions:
            next_node = node.transitions[tool_name]
            log.info(
                "flow.transition",
                from_node=self._context.current_node,
                to_node=next_node,
                trigger=tool_name,
            )
            self._context.current_node = next_node
            dest = self._config.nodes.get(next_node) if self._config.nodes else None
            if dest is not None and dest.terminal:
                self._ending = True
