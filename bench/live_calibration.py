"""Run the three bounded SIP calibration calls with webhook rollback.

The public tunnel and local onset server must already be running. This command
verifies both health endpoints before changing external state, snapshots the
existing Call Control application webhook, restores and verifies it in a
finally block, and stops after the first invalid capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from bench.acoustic_probe import ARTIFACT_ROOT, REPOSITORY_ROOT

API_BASE = "https://api.telnyx.com/v2"
CONTROL_MODES = ("agent-only", "no-stimulus", "echo-control")
CAPTURE_MODES = (*CONTROL_MODES, "measurement")


def _object(value: object, category: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(category)
    return value


class WebhookLease(AbstractAsyncContextManager[None]):
    """Temporarily change one application webhook and always restore it."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        application_id: str,
        temporary_url: str,
    ) -> None:
        self.client = client
        self.application_id = application_id
        self.temporary_url = temporary_url
        self.original_url = ""
        self.application_name = ""

    @property
    def endpoint(self) -> str:
        return f"/call_control_applications/{self.application_id}"

    async def _retrieve(self) -> dict[str, Any]:
        response = await self.client.get(self.endpoint)
        response.raise_for_status()
        root = _object(response.json(), "application_response")
        return _object(root.get("data"), "application_data")

    async def _set_and_verify(self, url: str) -> None:
        response = await self.client.patch(
            self.endpoint,
            json={
                "application_name": self.application_name,
                "webhook_event_url": url,
            },
        )
        response.raise_for_status()
        observed = await self._retrieve()
        if observed.get("webhook_event_url") != url:
            raise RuntimeError("webhook_verification_failed")

    async def __aenter__(self) -> None:
        current = await self._retrieve()
        original = current.get("webhook_event_url")
        name = current.get("application_name")
        if not isinstance(original, str) or not original:
            raise RuntimeError("original_webhook_missing")
        if not isinstance(name, str) or not name:
            raise RuntimeError("application_name_missing")
        self.original_url = original
        self.application_name = name
        await self._set_and_verify(self.temporary_url)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        if self.original_url:
            await self._set_and_verify(self.original_url)
        return None


def _load_environment(path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key, value in dotenv_values(path).items():
        if value is not None and key not in environment:
            environment[key] = value
    return environment


def _new_artifact(before: set[str]) -> Path:
    after = {
        path.name
        for path in ARTIFACT_ROOT.iterdir()
        if path.is_dir() and path.name.startswith("p2-")
    }
    created = after - before
    if len(created) != 1:
        raise RuntimeError("calibration_artifact_count_mismatch")
    return ARTIFACT_ROOT / created.pop()


def _validate_control_manifest(path: Path, mode: str) -> dict[str, object]:
    manifest = _object(
        json.loads((path / "manifest.json").read_text()), "manifest_invalid"
    )
    if manifest.get("capture_mode") != mode:
        raise RuntimeError("capture_mode_mismatch")
    if manifest.get("gate_outcome") != "CONTROL_CAPTURE_COMPLETE_PENDING_REVIEW":
        raise RuntimeError(f"control_capture_failed:{manifest.get('failure_category')}")
    if manifest.get("dirty_tree") is not False:
        raise RuntimeError("capture_dirty_tree")
    delivery = _object(
        manifest.get("tx_delivery_evidence"), "delivery_evidence_missing"
    )
    if mode == "echo-control" and delivery.get("confirmed") is not True:
        raise RuntimeError("echo_delivery_unconfirmed")
    if manifest.get("teardown_result") not in {"hangup_sent", "remote_bye"}:
        raise RuntimeError("control_teardown_unconfirmed")
    return manifest


def _validate_measurement_manifest(path: Path) -> dict[str, object]:
    manifest = _object(
        json.loads((path / "manifest.json").read_text()), "manifest_invalid"
    )
    if manifest.get("capture_mode") != "measurement":
        raise RuntimeError("capture_mode_mismatch")
    if manifest.get("gate_outcome") != "CAPTURE_COMPLETE_PENDING_REVIEW":
        raise RuntimeError(
            f"measurement_capture_failed:{manifest.get('failure_category')}"
        )
    if manifest.get("dirty_tree") is not False:
        raise RuntimeError("capture_dirty_tree")
    delivery = _object(
        manifest.get("tx_delivery_evidence"), "delivery_evidence_missing"
    )
    if delivery.get("confirmed") is not True:
        raise RuntimeError("measurement_delivery_unconfirmed")
    if manifest.get("teardown_result") not in {"hangup_sent", "remote_bye"}:
        raise RuntimeError("measurement_teardown_unconfirmed")
    return manifest


async def _health(client: httpx.AsyncClient, url: str) -> None:
    response = await client.get(url)
    response.raise_for_status()
    data = _object(response.json(), "health_invalid")
    if data.get("status") != "ok":
        raise RuntimeError("health_not_ok")


async def run(arguments: argparse.Namespace) -> list[dict[str, object]]:
    parsed = urlsplit(arguments.webhook_url)
    if parsed.scheme != "https" or parsed.path != "/webhook" or not parsed.netloc:
        raise RuntimeError("temporary_webhook_invalid")
    public_health = f"https://{parsed.netloc}/health"
    environment = _load_environment(REPOSITORY_ROOT / ".env")
    api_key = environment.get("TELNYX_API_KEY", "")
    application_id = environment.get("TELNYX_CONNECTION_ID", "")
    if not api_key or not application_id:
        raise RuntimeError("telnyx_configuration_missing")
    if arguments.attempt_start <= 0:
        raise RuntimeError("attempt_start_invalid")
    output = arguments.output
    if not output.is_absolute():
        output = REPOSITORY_ROOT / output
    if output.resolve().parent != ARTIFACT_ROOT.resolve():
        raise RuntimeError("session_output_must_be_in_artifact_root")
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = httpx.Timeout(20.0)
    results: list[dict[str, object]] = []
    async with (
        httpx.AsyncClient(timeout=timeout) as health_client,
        httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=timeout) as api,
    ):
        await _health(health_client, "http://127.0.0.1:8001/health")
        await _health(health_client, public_health)
        async with WebhookLease(api, application_id, arguments.webhook_url):
            for attempt_number, mode in enumerate(
                arguments.modes, start=arguments.attempt_start
            ):
                before = {
                    path.name
                    for path in ARTIFACT_ROOT.iterdir()
                    if path.is_dir() and path.name.startswith("p2-")
                }
                command = [
                    sys.executable,
                    "-m",
                    "bench.sip_harness",
                    "--live",
                    "--fixture",
                    str(arguments.fixture),
                    "--mode",
                    mode,
                    "--attempt-number",
                    str(attempt_number),
                    "--detector-window-ms",
                    str(arguments.detector_window_ms),
                    "--activity-threshold-dbfs",
                    str(arguments.activity_threshold_dbfs),
                    "--silence-threshold-dbfs",
                    str(arguments.silence_threshold_dbfs),
                    "--minimum-active-ms",
                    str(arguments.minimum_active_ms),
                    "--sustained-silence-ms",
                    str(arguments.sustained_silence_ms),
                ]
                call_environment = dict(environment)
                call_environment["BENCH_LIVE"] = "1"
                completed = subprocess.run(
                    command,
                    cwd=REPOSITORY_ROOT,
                    env=call_environment,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError(f"calibration_process_failed:{mode}")
                artifact = _new_artifact(before)
                manifest = (
                    _validate_measurement_manifest(artifact)
                    if mode == "measurement"
                    else _validate_control_manifest(artifact, mode)
                )
                results.append(
                    {
                        "mode": mode,
                        "run_id": artifact.name,
                        "terminal_outcome": manifest.get("terminal_outcome"),
                        "git_commit": manifest.get("git_commit"),
                    }
                )
    output.write_text(json.dumps({"captures": results}, indent=2) + "\n")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--modes", nargs="+", choices=CAPTURE_MODES, default=CONTROL_MODES
    )
    parser.add_argument("--attempt-start", type=int, default=1)
    parser.add_argument(
        "--output", type=Path, default=ARTIFACT_ROOT / "calibration_session.json"
    )
    parser.add_argument("--detector-window-ms", type=int, default=20)
    parser.add_argument("--activity-threshold-dbfs", type=float, default=-38.0)
    parser.add_argument("--silence-threshold-dbfs", type=float, default=-45.0)
    parser.add_argument("--minimum-active-ms", type=int, default=100)
    parser.add_argument("--sustained-silence-ms", type=int, default=500)
    arguments = parser.parse_args()
    results = asyncio.run(run(arguments))
    for result in results:
        print(f"{result['mode']}: {result['run_id']} ({result['terminal_outcome']})")


if __name__ == "__main__":
    main()
