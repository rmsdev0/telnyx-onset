"""Run a frozen Phase 3 manifest with restore-safe external state.

One server process is launched per scheduled trial because strict runtime
instrumentation permits exactly one call per process and one write-once event
file. Every scheduled attempt remains in the session record; failures are
classified and are never replaced. The caller fixture and all audio are
synthetic. The public tunnel must already be running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from bench.acoustic_probe import ARTIFACT_ROOT, REPOSITORY_ROOT
from bench.live_calibration import API_BASE, WebhookLease, _load_environment, _object
from bench.phase3 import (
    PHASE3_BARGE_OFFSET_MS,
    PHASE3_NATURAL_END_MS,
    TrialEvidence,
    classify_trial,
)
from onset.types import BenchmarkMode

PROFILE_PATH = REPOSITORY_ROOT / "bench" / "measurement_profile.json"
FIXTURE_PATH = ARTIFACT_ROOT / "fixtures" / "caller_table_for_two_v1.wav"


def _git_state() -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    clean = not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return commit, clean


def _validate_manifest_revision(declared: str, execution: str) -> None:
    """Allow only the commit that adds the frozen manifest/report after runtime."""
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", declared, execution],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("phase3 runtime revision is not an ancestor")
    changed = set(
        subprocess.run(
            ["git", "diff", "--name-only", f"{declared}..{execution}"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    allowed = {
        "bench/phase3_qualification_manifest.json",
        "bench/phase3_qualification_manifest_recalibration1.json",
        "bench/phase3_qualification_manifest_recalibration1b.json",
        "bench/phase3_final_manifest.json",
        "bench/PHASE3_REPORT.md",
    }
    if not changed <= allowed:
        raise RuntimeError("phase3 execution revision changes runtime files")


def _load_json(path: Path) -> dict[str, Any]:
    return _object(json.loads(path.read_text(encoding="utf-8")), "json_object_required")


def _load_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rows.append(_object(json.loads(line), "event_object_required"))
    return rows


def _new_artifact(before: set[str]) -> Path:
    after = {
        path.name
        for path in ARTIFACT_ROOT.iterdir()
        if path.is_dir() and path.name.startswith("p2-")
    }
    created = after - before
    if len(created) != 1:
        raise RuntimeError("phase3_artifact_count_mismatch")
    return ARTIFACT_ROOT / created.pop()


async def _wait_health(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("phase3_server_exited")
            try:
                response = await client.get(url)
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("phase3_server_health_timeout")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _agent_evidence(
    events: list[dict[str, Any]], condition: BenchmarkMode
) -> tuple[dict[str, object], str | None]:
    names = [str(row.get("event", "")) for row in events]
    ready = next(
        (row for row in events if row.get("event") == "benchmark_runtime_ready"),
        {},
    )
    expected_sources = (
        {"vad"}
        if condition == BenchmarkMode.ONSET_FD_VAD
        else {"transcript_interim", "transcript_final"}
    )
    interruptions = [
        row for row in events if row.get("event") == "interruption_requested"
    ]
    clears = [
        row for row in events if row.get("event") == "local_media_epoch_invalidated"
    ]
    turns = [row for row in events if row.get("event") == "caller_turn_completed"]
    next_responses = [
        row
        for row in events
        if row.get("event") == "next_response_started"
        and row.get("caller_turn_id") is not None
    ]
    turn_id = turns[0].get("turn_id") if len(turns) == 1 else None
    next_matches = (
        len(next_responses) == 1 and next_responses[0].get("caller_turn_id") == turn_id
    )
    timestamps = [row.get("monotonic_ns") for row in events]
    ordered = True
    previous: int | None = None
    for value in timestamps:
        if not isinstance(value, int) or (previous is not None and previous > value):
            ordered = False
            break
        previous = value
    return (
        {
            "event_names": set(names),
            "ready_condition": ready.get("condition"),
            "profile_sha256": ready.get("profile_sha256"),
            "matched_config_sha256": ready.get("matched_config_sha256"),
            "eligible_trigger_count": sum(
                row.get("source") in expected_sources for row in interruptions
            ),
            "interrupt_action_count": len(clears),
            "caller_turn_count": len(turns),
            "next_response_based_on_turn": next_matches,
            "event_order_valid": ordered,
            "clear_completed": any(
                row.get("event") == "clear_send_finished"
                and row.get("outcome") == "completed"
                for row in events
            ),
        },
        str(ready.get("matched_config_sha256"))
        if ready.get("matched_config_sha256")
        else None,
    )


def _stale_audio_resumed(
    artifact: Path,
    stop_ns: int,
    fixture_end_ns: int,
    *,
    sustained_silence_ms: int,
    activity_threshold_dbfs: float,
) -> bool:
    metadata_path = artifact / "frame_metadata.jsonl"
    if not metadata_path.exists():
        return False
    confirmation_end = stop_ns + sustained_silence_ms * 1_000_000
    for row in _load_events(metadata_path):
        host_ns = row.get("host_receive_monotonic_ns")
        rms = row.get("rms_dbfs")
        if (
            isinstance(host_ns, int)
            and isinstance(rms, int | float)
            and confirmation_end <= host_ns < fixture_end_ns
            and float(rms) >= activity_threshold_dbfs
        ):
            return True
    return False


def _classify(
    *,
    artifact: Path,
    harness: dict[str, Any],
    events: list[dict[str, Any]],
    trial: dict[str, Any],
    manifest: dict[str, Any],
    detector: dict[str, Any],
    expected_config_hash: str | None,
) -> tuple[dict[str, object], str | None]:
    condition = BenchmarkMode(str(trial["condition"]))
    agent, config_hash = _agent_evidence(events, condition)
    phase3 = harness.get("phase3")
    phase3 = phase3 if isinstance(phase3, dict) else {}
    delivery = harness.get("tx_delivery_evidence")
    delivery = delivery if isinstance(delivery, dict) else {}
    stop_ns = phase3.get("acoustic_stop_host_ns")
    fixture_end_ns = next(
        (
            int(row["host_monotonic_ns"])
            for row in _load_events(artifact / "events.jsonl")
            if row.get("event") == "stimulus_transmission_completed"
        ),
        0,
    )
    stale = (
        _stale_audio_resumed(
            artifact,
            stop_ns,
            fixture_end_ns,
            sustained_silence_ms=int(detector["sustained_silence_ms"]),
            activity_threshold_dbfs=float(detector["activity_threshold_dbfs"]),
        )
        if isinstance(stop_ns, int) and fixture_end_ns
        else False
    )
    required_names = agent["event_names"]
    assert isinstance(required_names, set)
    profile_record = manifest.get("measurement_profile")
    profile_record = profile_record if isinstance(profile_record, dict) else {}
    configuration_matches = bool(
        phase3.get("trial_id") == trial.get("trial_id")
        and phase3.get("condition") == condition.value
        and agent["ready_condition"] == condition.value
        and harness.get("git_commit") == manifest.get("execution_commit")
        and harness.get("dirty_tree") is False
        and config_hash is not None
        and agent["profile_sha256"] == profile_record.get("sha256")
        and (expected_config_hash is None or config_hash == expected_config_hash)
    )
    base_agent_events = {
        "benchmark_runtime_ready",
        "benchmark_call_claimed",
        "first_inbound_speech_bearing_frame",
        "response_teardown_completed",
    }
    instrumentation_complete = bool(
        required_names >= base_agent_events
        and phase3.get("stimulus_start_host_ns") is not None
    )
    action_count = agent["interrupt_action_count"]
    clear_required = isinstance(action_count, int) and action_count > 0
    transport_complete = bool(
        harness.get("teardown_result") in {"hangup_sent", "remote_bye"}
        and harness.get("rx_void_events", 99) <= 5
        and harness.get("rx_void_total_ms", 1001) <= 1000
        and (not clear_required or agent["clear_completed"])
    )
    evidence = TrialEvidence(
        agent_audio_active=phase3.get("agent_audio_active_at_stimulus") is True,
        stimulus_started=phase3.get("stimulus_start_host_ns") is not None,
        stimulus_delivered=delivery.get("confirmed") is True,
        media_capture_present=(artifact / "rx_8k.wav").is_file()
        and (artifact / "tx_8k.wav").is_file(),
        configuration_matches=configuration_matches,
        event_order_valid=bool(agent["event_order_valid"]),
        instrumentation_complete=instrumentation_complete,
        transport_complete=transport_complete,
        eligible_trigger_count=(
            agent["eligible_trigger_count"]
            if isinstance(agent["eligible_trigger_count"], int)
            else 0
        ),
        interrupt_action_count=(
            agent["interrupt_action_count"]
            if isinstance(agent["interrupt_action_count"], int)
            else 0
        ),
        acoustic_stop_found=stop_ns is not None,
        stop_before_natural_end=phase3.get("stop_before_natural_end") is True,
        caller_turn_count=(
            agent["caller_turn_count"]
            if isinstance(agent["caller_turn_count"], int)
            else 0
        ),
        stale_audio_resumed=stale,
        next_response_based_on_turn=bool(agent["next_response_based_on_turn"]),
        ended_naturally=(
            stop_ns is not None and phase3.get("stop_before_natural_end") is not True
        ),
    )
    classification = classify_trial(evidence)
    record: dict[str, object] = {
        "schema_version": 1,
        "trial_id": trial["trial_id"],
        "attempt_index": trial["attempt_index"],
        "condition": condition.value,
        "harness_run_id": artifact.name,
        "harness_terminal_outcome": harness.get("terminal_outcome"),
        "evidence": asdict(evidence),
        "classification": {
            "eligible": classification.eligible,
            "successful": classification.successful,
            "failure_codes": list(classification.failure_codes),
        },
        "metrics": {
            "harness_boundary_latency_ms": phase3.get("harness_boundary_latency_ms"),
            "stimulus_start_host_ns": phase3.get("stimulus_start_host_ns"),
            "acoustic_stop_host_ns": stop_ns,
        },
        "agent_matched_config_sha256": config_hash,
    }
    return record, config_hash


def _write_json_exclusive(path: Path, value: object) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()


async def run(arguments: argparse.Namespace) -> list[dict[str, object]]:
    if not arguments.live or os.environ.get("PHASE3_LIVE") != "1":
        raise RuntimeError("phase3 live gates are closed")
    manifest = _load_json(arguments.manifest)
    profile = _load_json(PROFILE_PATH)
    detector = _object(profile.get("detector"), "profile detector missing")
    commit, clean = _git_state()
    manifest_git = manifest.get("git")
    manifest_git = manifest_git if isinstance(manifest_git, dict) else {}
    declared_commit = manifest_git.get("commit")
    if not clean or not isinstance(declared_commit, str):
        raise RuntimeError("phase3 manifest revision mismatch")
    _validate_manifest_revision(declared_commit, commit)
    manifest["execution_commit"] = commit
    if manifest.get("stage") not in {"qualification", "final"}:
        raise RuntimeError("phase3 manifest stage invalid")
    trials = manifest.get("trials")
    if not isinstance(trials, list) or not trials:
        raise RuntimeError("phase3 manifest trials missing")
    parsed = urlsplit(arguments.webhook_url)
    if parsed.scheme != "https" or parsed.path != "/webhook" or not parsed.netloc:
        raise RuntimeError("temporary webhook invalid")
    media_url = f"wss://{parsed.netloc}/ws/media"
    public_health = f"https://{parsed.netloc}/health"
    environment = _load_environment(REPOSITORY_ROOT / ".env")
    api_key = environment.get("TELNYX_API_KEY", "")
    application_id = environment.get("TELNYX_CONNECTION_ID", "")
    if not api_key or not application_id:
        raise RuntimeError("telnyx configuration missing")
    headers = {"Authorization": f"Bearer {api_key}"}
    results: list[dict[str, object]] = []
    matched_config_hash: str | None = None
    async with (
        httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=20) as api,
        WebhookLease(api, application_id, arguments.webhook_url),
    ):
        for raw_trial in trials:
            trial = _object(raw_trial, "phase3 trial invalid")
            trial_id = str(trial.get("trial_id", ""))
            condition = str(trial.get("condition", ""))
            before = {
                path.name
                for path in ARTIFACT_ROOT.iterdir()
                if path.is_dir() and path.name.startswith("p2-")
            }
            event_path = ARTIFACT_ROOT / f"{trial_id}-agent-events.jsonl"
            server_log_path = ARTIFACT_ROOT / f"{trial_id}-server.log"
            if event_path.exists() or server_log_path.exists():
                raise RuntimeError(f"phase3 trial evidence already exists:{trial_id}")
            server_environment = dict(environment)
            server_environment.update(
                {
                    "HOST": "127.0.0.1",
                    "PORT": "8001",
                    "MEDIA_STREAM_URL": media_url,
                    "HALF_DUPLEX": "false",
                    "BENCHMARK_MODE": condition,
                    "BENCHMARK_TRIAL_ID": trial_id,
                    "BENCHMARK_EVENTS_PATH": str(event_path),
                    "BENCHMARK_PROFILE_PATH": str(PROFILE_PATH),
                    "TTS_STREAMING_DECODE": "false",
                }
            )
            log_fd = os.open(
                server_log_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(log_fd, "wb") as log_handle:
                server = subprocess.Popen(
                    [sys.executable, "-m", "onset"],
                    cwd=REPOSITORY_ROOT,
                    env=server_environment,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                try:
                    await _wait_health("http://127.0.0.1:8001/health", server)
                    await _wait_health(public_health, server)
                    command = [
                        sys.executable,
                        "-m",
                        "bench.sip_harness",
                        "--live",
                        "--fixture",
                        str(arguments.fixture),
                        "--mode",
                        "phase3",
                        "--attempt-number",
                        str(trial["attempt_index"]),
                        "--phase3-trial-id",
                        trial_id,
                        "--phase3-condition",
                        condition,
                        "--activity-threshold-dbfs",
                        str(detector["activity_threshold_dbfs"]),
                        "--silence-threshold-dbfs",
                        str(detector["silence_threshold_dbfs"]),
                        "--minimum-active-ms",
                        str(detector["minimum_active_ms"]),
                        "--sustained-silence-ms",
                        str(detector["sustained_silence_ms"]),
                        "--barge-offset-ms",
                        str(PHASE3_BARGE_OFFSET_MS),
                        "--natural-end-ms",
                        str(PHASE3_NATURAL_END_MS),
                    ]
                    call_environment = dict(environment)
                    call_environment["BENCH_LIVE"] = "1"
                    completed = subprocess.run(
                        command,
                        cwd=REPOSITORY_ROOT,
                        env=call_environment,
                        check=False,
                    )
                finally:
                    _stop_process(server)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"phase3_harness_process_failed:{trial_id}:{completed.returncode}"
                )
            artifact = _new_artifact(before)
            server_log_path.replace(artifact / "server.log")
            if not event_path.exists():
                raise RuntimeError(f"phase3 agent events missing:{trial_id}")
            moved_events = artifact / "agent_events.jsonl"
            event_path.replace(moved_events)
            harness = _load_json(artifact / "manifest.json")
            events = _load_events(moved_events)
            record, observed_hash = _classify(
                artifact=artifact,
                harness=harness,
                events=events,
                trial=trial,
                manifest=manifest,
                detector=detector,
                expected_config_hash=matched_config_hash,
            )
            if matched_config_hash is None:
                matched_config_hash = observed_hash
            record["harness_process_returncode"] = completed.returncode
            _write_json_exclusive(artifact / "phase3_trial.json", record)
            results.append(record)
            classification = _object(record["classification"], "classification invalid")
            print(
                f"{trial_id} {condition}: "
                f"eligible={classification['eligible']} "
                f"successful={classification['successful']} "
                f"run={artifact.name}",
                flush=True,
            )
    _write_json_exclusive(
        arguments.output,
        {
            "schema_version": 1,
            "manifest": str(arguments.manifest),
            "completed_utc": datetime.now(UTC).isoformat(),
            "trials": results,
        },
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=ARTIFACT_ROOT / "phase3_qualification_session.json",
    )
    arguments = parser.parse_args()
    results = asyncio.run(run(arguments))
    successful = sum(
        bool(_object(row["classification"], "classification").get("successful"))
        for row in results
    )
    print(f"Phase 3 completed: {successful}/{len(results)} successful")


if __name__ == "__main__":
    main()
