"""Telnyx client tests: streaming_start payload and webhook verification."""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest
from nacl.encoding import Base64Encoder
from nacl.signing import SigningKey

from onset.settings import Settings
from onset.telnyx import Call, verify_webhook


class StubClient:
    """Records Call Control actions instead of issuing them."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def action(
        self, call_control_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((call_control_id, action, payload))
        return {}


@pytest.mark.asyncio
async def test_start_streaming_payload_matches_l16_16k() -> None:
    settings = Settings(
        telnyx_api_key="k", telnyx_public_key="p", media_codec="L16", sample_rate=16000
    )
    stub = StubClient(settings)
    call = Call(stub, "cc-1")  # type: ignore[arg-type]
    await call.start_streaming("wss://host/ws/media")

    assert len(stub.calls) == 1
    ccid, action, payload = stub.calls[0]
    assert ccid == "cc-1"
    assert action == "streaming_start"
    assert payload == {
        "stream_url": "wss://host/ws/media",
        "stream_track": "inbound_track",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "L16",
        # The sampling rate must be set: it defaults to 8000 and would otherwise
        # mismatch the 16 kHz pipeline.
        "stream_bidirectional_sampling_rate": 16000,
        # "self" so the caller hears the agent on a single answered call.
        "stream_bidirectional_target_legs": "self",
    }


def _sign(body: bytes, timestamp: str, sk: SigningKey) -> str:
    signed = f"{timestamp}|".encode() + body
    return base64.b64encode(sk.sign(signed).signature).decode()


def test_verify_webhook_accepts_valid_and_rejects_tampered() -> None:
    sk = SigningKey.generate()
    public_key = sk.verify_key.encode(Base64Encoder).decode()
    body = b'{"data":{"event_type":"call.initiated"}}'
    ts = str(int(time.time()))
    headers = {
        "telnyx-signature-ed25519": _sign(body, ts, sk),
        "telnyx-timestamp": ts,
    }

    assert verify_webhook(public_key, headers, body)
    # Tampered body fails.
    assert not verify_webhook(public_key, headers, body + b"x")
    # Stale timestamp fails.
    old_ts = str(int(time.time()) - 10_000)
    old_headers = {
        "telnyx-signature-ed25519": _sign(body, old_ts, sk),
        "telnyx-timestamp": old_ts,
    }
    assert not verify_webhook(public_key, old_headers, body)
    # Missing headers fail.
    assert not verify_webhook(public_key, {}, body)
