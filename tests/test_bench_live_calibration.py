from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from bench.live_calibration import WebhookLease, _validate_control_manifest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_webhook_lease_restores_after_failure() -> None:
    state = {"url": "https://original.example/webhook"}
    patches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "application_name": "agent",
                        "webhook_event_url": state["url"],
                    }
                },
            )
        body = json.loads(request.content)
        state["url"] = body["webhook_event_url"]
        patches.append(state["url"])
        return httpx.Response(200, json={"data": {}})

    transport = httpx.MockTransport(handler)
    with pytest.raises(RuntimeError, match="call failed"):
        async with httpx.AsyncClient(
            base_url="https://api.telnyx.com/v2", transport=transport
        ) as client:
            async with WebhookLease(
                client, "app", "https://temporary.example/webhook"
            ):
                raise RuntimeError("call failed")
    assert patches == [
        "https://temporary.example/webhook",
        "https://original.example/webhook",
    ]
    assert state["url"] == "https://original.example/webhook"


def test_control_manifest_requires_echo_delivery(tmp_path: Path) -> None:
    run = tmp_path / "p2-aaaaaaaaaaaaaaaa"
    run.mkdir()
    manifest = {
        "capture_mode": "echo-control",
        "gate_outcome": "CONTROL_CAPTURE_COMPLETE_PENDING_REVIEW",
        "dirty_tree": False,
        "teardown_result": "hangup_sent",
        "tx_delivery_evidence": {"confirmed": False},
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="echo_delivery_unconfirmed"):
        _validate_control_manifest(run, "echo-control")
