"""Server tests: helpers and the media WebSocket route (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from onset.server import (
    _dispatch,
    _is_ours,
    _matches_connection,
    app,
    parse_speak_generation,
)
from onset.settings import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


class _StubCall:
    def __init__(self, rec: list[object]) -> None:
        self.rec = rec

    async def answer(self) -> None:
        self.rec.append("answer")

    async def start_streaming(self, url: str) -> None:
        self.rec.append(("stream", url))

    async def hangup(self) -> None:
        self.rec.append("hangup")


class _StubTelnyx:
    def __init__(self) -> None:
        self.rec: list[object] = []

    def call(self, ccid: str) -> _StubCall:
        return _StubCall(self.rec)


@pytest.fixture
def telnyx_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The lifespan builds real Settings, which require the Telnyx keys.
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    monkeypatch.setenv("TELNYX_PUBLIC_KEY", "test-pub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_parse_speak_generation() -> None:
    assert parse_speak_generation("speak:7") == 7
    assert parse_speak_generation("speak:notanint") is None
    assert parse_speak_generation("other") is None


def test_is_ours_gates_on_connection_and_direction() -> None:
    s = Settings(
        telnyx_api_key="k", telnyx_public_key="p", telnyx_connection_id="conn1"
    )
    assert _is_ours(s, {"direction": "incoming", "connection_id": "conn1"})
    assert not _is_ours(s, {"direction": "incoming", "connection_id": "other"})
    assert not _is_ours(s, {"direction": "outgoing", "connection_id": "conn1"})


def test_matches_connection_is_lenient_on_missing_id() -> None:
    s = Settings(
        telnyx_api_key="k", telnyx_public_key="p", telnyx_connection_id="conn1"
    )
    assert _matches_connection(s, {"connection_id": "conn1"})
    assert not _matches_connection(s, {"connection_id": "other"})
    # An event that omits connection_id (e.g. call.answered) is not rejected.
    assert _matches_connection(s, {})


@pytest.mark.asyncio
async def test_dispatch_streams_on_answered_without_direction() -> None:
    # Regression: call.answered carries no direction, so streaming must start on
    # the connection match alone (the bug that left callers in dead air).
    s = Settings(
        telnyx_api_key="k",
        telnyx_public_key="p",
        telnyx_connection_id="conn1",
        media_stream_url="wss://host/ws/media",
    )
    telnyx = _StubTelnyx()
    fake_app = cast(
        "FastAPI",
        SimpleNamespace(
            state=SimpleNamespace(settings=s, telnyx=telnyx, stream_tokens={})
        ),
    )
    await _dispatch(fake_app, "call.answered", {"connection_id": "conn1"}, "cc-1")
    streamed = [r for r in telnyx.rec if isinstance(r, tuple)]
    assert len(streamed) == 1
    assert streamed[0][0] == "stream"
    # The stream URL carries the per-call auth token.
    assert streamed[0][1].startswith("wss://host/ws/media")
    assert "token=" in streamed[0][1]


def test_health(telnyx_env: None) -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_media_ws_rejects_unauthenticated(telnyx_env: None) -> None:
    # No token: the media socket must reject the connection before doing any work,
    # so it cannot consume a slot or open provider sockets.
    from starlette.websockets import WebSocketDisconnect

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/media") as ws,
    ):
        ws.receive_text()  # the server closed us with 1008


def test_media_ws_rejects_ccid_mismatch(telnyx_env: None) -> None:
    # A valid token but a Start carrying a different call_control_id is rejected,
    # and no agent is ever built.
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as client:
        app.state.stream_tokens["tok-test"] = "cc-real"
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/media?token=tok-test") as ws,
        ):
            ws.send_text(
                json.dumps(
                    {
                        "event": "start",
                        "start": {"call_control_id": "cc-evil", "from": "+1"},
                        "stream_id": "s",
                    }
                )
            )
            ws.receive_text()
        assert app.state.agents == {}
