"""FastAPI server: call-control webhooks plus the media WebSocket route.

Two surfaces, one call:

- /webhook receives signed Telnyx call-control events. On an inbound call it
  answers, and on call.answered it issues streaming_start, which makes Telnyx
  dial the media WebSocket below.
- /ws/media is where Telnyx streams the call's bidirectional RTP audio. Its read
  loop owns the inbound socket: it spins up the call's MediaStream, STT, TTS, and
  VoiceAgent on the start event, routes inbound frames to the agent, and tears
  everything down cleanly on stop, disconnect, or error.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from onset.agent import VoiceAgent
from onset.limits import CallLimiter
from onset.logging import setup_logging
from onset.media import Connected, Dtmf, Mark, Media, MediaStream, Start, Stop, decode
from onset.prompts import RESTAURANT_CONFIG
from onset.settings import get_settings
from onset.telnyx import TelnyxClient, verify_webhook

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from onset.settings import Settings

log = structlog.get_logger()

# Telnyx call webhooks are a few KB. Cap the body so an unauthenticated client
# (who cannot forge a valid Ed25519 signature) cannot exhaust memory on the one
# public endpoint, since the body must be buffered before it is verified.
MAX_WEBHOOK_BYTES = 256 * 1024


def parse_speak_generation(mark_name: str) -> int | None:
    """Recover the speak generation from a mark name like 'speak:7'.

    None if it cannot be read, which the agent treats as 'not the live speak' so
    an unexpected mark never completes a turn.
    """
    if mark_name.startswith("speak:"):
        with contextlib.suppress(ValueError):
            return int(mark_name.split(":", 1)[1])
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    setup_logging(settings)
    app.state.settings = settings
    app.state.telnyx = TelnyxClient(settings)
    app.state.limiter = CallLimiter(settings.max_concurrent_calls)
    # call_control_id -> VoiceAgent. Single event loop, so a plain dict is safe.
    app.state.agents = {}
    # One-time media-socket tokens: token -> call_control_id, minted at
    # streaming_start and consumed when Telnyx dials /ws/media. Authenticates the
    # otherwise-public media socket.
    app.state.stream_tokens = {}
    log.info("onset.startup", env=settings.env, port=settings.port)
    try:
        yield
    finally:
        agents: dict[str, VoiceAgent] = app.state.agents
        for agent in list(agents.values()):
            task = agent.run_task
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            else:
                await agent.aclose()
        agents.clear()
        await app.state.telnyx.aclose()
        log.info("onset.shutdown")


app = FastAPI(title="telnyx-onset", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Call-control webhooks ────────────────────────────────────────


def _matches_connection(settings: Settings, payload: dict[str, Any]) -> bool:
    """True unless the webhook is explicitly for a different connection.

    The webhook URL is configured per Voice API application, so every webhook we
    receive is already for our app. This only rejects an explicit connection_id
    mismatch; events that omit connection_id (for example call.answered) are not
    rejected.
    """
    cid = payload.get("connection_id", "")
    if not settings.telnyx_connection_id or not cid:
        return True
    return bool(cid == settings.telnyx_connection_id)


def _is_ours(settings: Settings, payload: dict[str, Any]) -> bool:
    """True for an inbound call we should answer: our connection, incoming."""
    return _matches_connection(settings, payload) and bool(
        payload.get("direction", "") == "incoming"
    )


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Receive, verify, and dispatch a Telnyx call webhook."""
    settings = request.app.state.settings

    raw_body = b""
    async for chunk in request.stream():
        raw_body += chunk
        if len(raw_body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")

    if not verify_webhook(
        settings.telnyx_public_key,
        request.headers,
        raw_body,
        settings.webhook_tolerance_s,
    ):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = json.loads(raw_body)["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("webhook.malformed")
        return Response(status_code=200)

    event_type = data.get("event_type", "")
    payload = data.get("payload", {})
    call_control_id = payload.get("call_control_id", "")
    structlog.contextvars.bind_contextvars(call_sid=call_control_id)
    try:
        await _dispatch(request.app, event_type, payload, call_control_id)
    except Exception:
        # A handler failure must not return 5xx and trigger Telnyx retries that
        # re-run side effects; log and acknowledge.
        log.exception("webhook.dispatch_failed", event_type=event_type)
    finally:
        structlog.contextvars.unbind_contextvars("call_sid")
    return Response(status_code=200)


def _mint_stream_url(app: FastAPI, settings: Settings, call_control_id: str) -> str:
    """Mint a one-time token for this call and append it to the media stream URL.

    The token authenticates the media socket: only a connection presenting a
    token we minted (and carrying the matching call_control_id) is served.
    """
    tokens: dict[str, str] = app.state.stream_tokens
    # Bound memory against answered-but-never-connected calls.
    while len(tokens) > 256:
        tokens.pop(next(iter(tokens)), None)
    token = secrets.token_urlsafe(24)
    tokens[token] = call_control_id
    base = settings.media_stream_url
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={token}"


async def _dispatch(
    app: FastAPI, event_type: str, payload: dict[str, Any], call_control_id: str
) -> None:
    settings = app.state.settings
    telnyx: TelnyxClient = app.state.telnyx

    # One info line per webhook, so the whole call flow is visible in the log.
    log.info(
        "webhook.received", event_type=event_type, direction=payload.get("direction")
    )

    if event_type == "call.initiated":
        if not _is_ours(settings, payload):
            return
        try:
            await telnyx.call(call_control_id).answer()
            log.info("call.answer_sent")
        except Exception:
            log.exception("call.answer_failed")

    elif event_type == "call.answered":
        # Gate on connection only: call.answered carries no direction, so a
        # direction check here would silently skip streaming_start.
        if not _matches_connection(settings, payload):
            return
        if not settings.media_stream_url:
            log.error("call.no_media_stream_url")
            return
        try:
            stream_url = _mint_stream_url(app, settings, call_control_id)
            await telnyx.call(call_control_id).start_streaming(stream_url)
            log.info("call.streaming_started", stream_url=settings.media_stream_url)
        except Exception:
            log.exception("call.streaming_start_failed")
            with contextlib.suppress(Exception):
                await telnyx.call(call_control_id).hangup()

    elif event_type == "call.hangup":
        # The media route owns teardown once streaming starts; this is just for
        # the log and for calls that hung up before the media socket connected.
        log.info(
            "call.hangup",
            cause=payload.get("hangup_cause"),
            source=payload.get("hangup_source"),
        )


# ── Media WebSocket ──────────────────────────────────────────────


@app.websocket("/ws/media")
async def media_ws(ws: WebSocket) -> None:
    """Telnyx's bidirectional RTP stream for one call.

    The read loop spins up the call's sockets and agent on the start event, then
    routes inbound frames; the finally block tears everything down on every exit
    path (stop, disconnect, or error) with no orphaned tasks or leaked slots.
    """
    await ws.accept()
    settings = ws.app.state.settings
    telnyx: TelnyxClient = ws.app.state.telnyx
    limiter: CallLimiter = ws.app.state.limiter
    agents: dict[str, VoiceAgent] = ws.app.state.agents
    tokens: dict[str, str] = ws.app.state.stream_tokens

    # Authenticate the media socket: Telnyx dials the tokenized URL minted at
    # streaming_start. Reject anything without a token we issued, before it can
    # acquire a slot or open provider sockets.
    token = ws.query_params.get("token", "")
    expected_ccid = tokens.pop(token, None) if token else None
    if expected_ccid is None:
        log.warning("media.rejected_unauthenticated")
        with contextlib.suppress(Exception):
            await ws.close(code=1008)
        return

    agent: VoiceAgent | None = None
    media: MediaStream | None = None
    call_control_id = ""
    acquired = False

    try:
        while True:
            raw = await ws.receive_text()
            event = decode(raw)

            if isinstance(event, Start):
                if agent is not None:
                    continue
                if event.call_control_id != expected_ccid:
                    log.warning("media.ccid_mismatch", ccid=event.call_control_id)
                    break
                call_control_id = event.call_control_id
                if not limiter.try_acquire():
                    log.warning("media.rejected_at_capacity")
                    with contextlib.suppress(Exception):
                        await telnyx.call(call_control_id).hangup()
                    break
                acquired = True
                media = MediaStream(
                    ws,
                    frame_ms=settings.frame_ms,
                    lead_frames=settings.inject_lead_frames,
                )
                media.start()
                agent = VoiceAgent(
                    settings,
                    telnyx.call(call_control_id),
                    media,
                    RESTAURANT_CONFIG,
                )
                agent.set_call_info(call_control_id, event.from_number)
                media.on_error = agent._on_socket_error
                agents[call_control_id] = agent
                agent.start()
                log.info("media.started", ccid=call_control_id, active=limiter.active)

            elif isinstance(event, Media):
                if agent is not None:
                    agent.handle_audio(event.pcm16)

            elif isinstance(event, Mark):
                if agent is not None:
                    agent.submit_speak_ended(parse_speak_generation(event.name))

            elif isinstance(event, Stop):
                log.info("media.stop")
                break

            elif isinstance(event, Connected):
                log.info("media.connected", version=event.version)

            elif isinstance(event, Dtmf):
                log.info("media.dtmf", digit=event.digit)

            else:
                log.debug("media.ignored", reason=getattr(event, "reason", ""))

    except WebSocketDisconnect:
        log.info("media.disconnected")
    except Exception:
        log.exception("media.loop_failed")
    finally:
        if agent is not None:
            agent.submit_hangup()
            task = agent.run_task
            if task is not None:
                # Not shielded: if the run task does not wind down within the
                # timeout, wait_for cancels it, which runs its teardown rather
                # than leaving it orphaned.
                with contextlib.suppress(
                    TimeoutError, asyncio.CancelledError, Exception
                ):
                    await asyncio.wait_for(task, timeout=10)
            else:
                await agent.aclose()
            agents.pop(call_control_id, None)
        if media is not None:
            await media.aclose()
        if acquired:
            limiter.release()
        with contextlib.suppress(Exception):
            await ws.close()
