"""Thin typed wrappers over the Telnyx primitives this agent uses.

One Telnyx API key authenticates all of them. The Call Control commands set the
call up and start the bidirectional media stream; the OpenAI-compatible client
points at Telnyx inference; Ed25519 verification authenticates inbound webhooks.
The audio itself flows over the media, STT, and TTS WebSockets (see media.py,
stt.py, tts.py), not through Call Control.
"""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from nacl.encoding import Base64Encoder
from nacl.signing import VerifyKey
from openai import AsyncOpenAI

from onset.types import LLMEvent, LLMEventType, ToolCallRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from onset.settings import Settings
    from onset.types import LLMMessage

log = structlog.get_logger()

# Telnyx error code for a command issued to a call that has already ended. A
# benign teardown race (the caller hung up while we were cleaning up), logged at
# info rather than error.
CALL_ENDED_ERROR_CODE = "90018"


def _is_call_ended_error(resp: httpx.Response) -> bool:
    """True if Telnyx rejected the action because the call already ended."""
    if resp.status_code != 422:
        return False
    try:
        errors = resp.json().get("errors", [])
    except Exception:
        return False
    return any(str(e.get("code")) == CALL_ENDED_ERROR_CODE for e in errors)


# ── Webhook verification ─────────────────────────────────────────


def verify_webhook(
    public_key: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    tolerance_s: int = 300,
) -> bool:
    """Verify a Telnyx webhook's Ed25519 signature.

    Telnyx signs every webhook with Ed25519 over "{timestamp}|{body}", carried
    in the telnyx-signature-ed25519 and telnyx-timestamp headers. Any failure
    (missing headers, stale timestamp, bad signature) means it is not trusted.
    """
    signature = headers.get("telnyx-signature-ed25519")
    timestamp = headers.get("telnyx-timestamp")
    if not signature or not timestamp:
        log.warning("telnyx.webhook_missing_headers")
        return False

    try:
        ts = int(timestamp)
    except ValueError:
        log.warning("telnyx.webhook_bad_timestamp")
        return False

    if abs(time.time() - ts) > tolerance_s:
        log.warning("telnyx.webhook_stale", age_s=round(abs(time.time() - ts)))
        return False

    signed = f"{timestamp}|".encode() + raw_body
    try:
        verify_key = VerifyKey(public_key.encode(), encoder=Base64Encoder)
        verify_key.verify(signed, base64.b64decode(signature))
    except Exception as e:
        log.warning("telnyx.signature_rejected", error=str(e))
        return False
    return True


# ── LLM (OpenAI-compatible Telnyx inference) ─────────────────────


class TelnyxLLM:
    """Streaming chat completions against Telnyx inference.

    The streaming and tool-call accumulation logic is lifted from the
    voice-agent-lite OpenAI-compatible provider: the only single-vendor changes
    are the base URL, the pinned model, and enable_thinking disabled for
    real-time voice latency.
    """

    def __init__(self, settings: Settings) -> None:
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self._client = AsyncOpenAI(
            api_key=settings.telnyx_api_key,
            base_url=settings.llm_base_url,
        )

    async def stream_response(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a chat completion, yielding text deltas and tool calls."""
        api_messages: list[dict[str, Any]] = []

        if system:
            api_messages.append({"role": "system", "content": system})

        for m in messages:
            msg: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            api_messages.append(msg)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "temperature": self.temperature,
            "stream": True,
            # Telnyx extension: skip the model's internal reasoning step so the
            # first token arrives fast enough for a live phone call.
            "extra_body": {"enable_thinking": False},
        }
        if tools:
            kwargs["tools"] = tools
        else:
            # Telnyx rejects max_tokens alongside function tools (error 10015),
            # so the per-response length cap is only applied on tool-free turns.
            kwargs["max_tokens"] = self.max_tokens

        stream = await self._client.chat.completions.create(**kwargs)

        # Accumulate tool calls across chunks (they arrive in fragments)
        tool_call_accumulators: dict[int, dict[str, str]] = {}

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue

            delta = choice.delta

            if delta and delta.content:
                yield LLMEvent(type=LLMEventType.TEXT_DELTA, text=delta.content)

            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_accumulators:
                        tool_call_accumulators[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    acc = tool_call_accumulators[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["arguments"] += tc_delta.function.arguments

        # Emit accumulated tool calls once the stream ends. Flushing here, rather
        # than only on finish_reason == "tool_calls", tolerates backends that end
        # a tool turn with finish_reason "stop" or simply close the stream (a
        # documented cross-model inconsistency, relevant to the K2.5 to K2.6 move).
        for idx in sorted(tool_call_accumulators):
            acc = tool_call_accumulators[idx]
            yield LLMEvent(
                type=LLMEventType.TOOL_CALL,
                tool_call=ToolCallRequest(
                    id=acc["id"],
                    name=acc["name"],
                    arguments=acc["arguments"],
                ),
            )

    async def aclose(self) -> None:
        await self._client.close()


# ── Call Control ─────────────────────────────────────────────────


class Call:
    """Call Control commands scoped to one call_control_id.

    Every command is a POST to /v2/calls/{id}/actions/{action}. Failures raise
    httpx.HTTPStatusError; callers decide whether to degrade or tear down. The
    media-stream build only needs answer, streaming_start, and hangup; the audio
    runs over the media socket, not speak/playback commands.
    """

    def __init__(self, client: TelnyxClient, call_control_id: str) -> None:
        self._client = client
        self.id = call_control_id

    async def answer(self) -> None:
        await self._client.action(self.id, "answer")

    async def start_streaming(self, stream_url: str) -> None:
        """Begin one bidirectional RTP media stream to stream_url.

        inbound_track streams the caller's audio for STT and VAD; outbound audio
        is injected by us on the same socket. The codec is L16 (16 kHz linear
        PCM), so no audio is transcoded between the media socket and the STT
        socket. Telnyx transcodes between the 8 kHz PSTN leg and this stream.
        """
        s = self._client.settings
        # stream_bidirectional_sampling_rate defaults to 8000; it MUST be set to
        # match the L16 16 kHz pipeline, or inbound frames and injected audio are
        # read at the wrong rate. target_legs defaults to "opposite", which on a
        # single answered inbound call targets a non-existent leg and leaves the
        # caller in silence, so we inject onto "self" (settings default).
        await self._client.action(
            self.id,
            "streaming_start",
            {
                "stream_url": stream_url,
                "stream_track": "inbound_track",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": s.media_codec,
                "stream_bidirectional_sampling_rate": s.sample_rate,
                "stream_bidirectional_target_legs": s.stream_bidirectional_target_legs,
            },
        )

    async def hangup(self) -> None:
        await self._client.action(self.id, "hangup")


class TelnyxClient:
    """Process-wide Telnyx Call Control client.

    Holds one authenticated httpx session, issues outbound dials, and mints a
    Call handle per call_control_id.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.telnyx_api_base,
            headers={"Authorization": f"Bearer {settings.telnyx_api_key}"},
            timeout=httpx.Timeout(10.0),
        )

    def call(self, call_control_id: str) -> Call:
        return Call(self, call_control_id)

    async def action(
        self, call_control_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await self._http.post(
            f"/calls/{call_control_id}/actions/{action}", json=payload or {}
        )
        if resp.is_error:
            if _is_call_ended_error(resp):
                log.info("telnyx.action_skipped_call_ended", action=action)
            else:
                # Surface Telnyx's explanation (a 422 names the offending field)
                # before raising, so failures are diagnosable from the logs.
                log.error(
                    "telnyx.action_failed",
                    action=action,
                    status=resp.status_code,
                    body=resp.text[:1000],
                )
            resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def dial(self, to: str, *, connection_id: str = "", from_: str = "") -> str:
        """Place an outbound call and return its call_control_id."""
        body = {
            "connection_id": connection_id or self.settings.telnyx_connection_id,
            "to": to,
            "from": from_ or self.settings.telnyx_phone_number,
        }
        resp = await self._http.post("/calls", json=body)
        resp.raise_for_status()
        ccid: str = resp.json()["data"]["call_control_id"]
        log.info("call.dialed", to=to, call_control_id=ccid)
        return ccid

    async def aclose(self) -> None:
        await self._http.aclose()
