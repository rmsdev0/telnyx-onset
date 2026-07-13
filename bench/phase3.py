"""Phase 3 run ordering, immutable trial records, and classification.

This module does not place calls. It prospectively freezes attempted-trial
order and classifies evidence produced by the separately gated live harness.
Qualification and final manifests are deliberately separate populations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from onset.types import BenchmarkMode

PRIMARY_CONDITIONS = (
    BenchmarkMode.ONSET_FD_VAD,
    BenchmarkMode.ONSET_FD_TRANSCRIPT,
)
DEFAULT_ORDER_BLOCK_PER_CONDITION = 2
QUALIFICATION_ATTEMPTS_PER_CONDITION = 10
FINAL_ATTEMPTS_PER_CONDITION = 40
PHASE3_BARGE_OFFSET_MS = 1_000
PHASE3_NATURAL_END_MS = 3_200
PHASE3_NATURAL_END_RUN_ID = "p2-1de191a7ca4e9f33"


class FailureCode(StrEnum):
    AGENT_NEVER_SPOKE = "agent_never_spoke"
    STIMULUS_STARTED_WITHOUT_AGENT_AUDIO = "stimulus_started_without_agent_audio"
    STIMULUS_DELIVERY_FAILED = "stimulus_delivery_failed"
    MEDIA_CAPTURE_MISSING = "media_capture_missing"
    TRIGGER_NOT_OBSERVED = "trigger_not_observed"
    INTERRUPT_ACTION_NOT_OBSERVED = "interrupt_action_not_observed"
    DUPLICATE_INTERRUPT_ACTION = "duplicate_interrupt_action"
    NATURAL_UTTERANCE_END = "natural_utterance_end"
    ACOUSTIC_STOP_NOT_FOUND = "acoustic_stop_not_found"
    CALLER_TURN_LOST = "caller_turn_lost"
    CALLER_TURN_DUPLICATED = "caller_turn_duplicated"
    STALE_AUDIO_RESUMED = "stale_audio_resumed"
    FALSE_INTERRUPTION = "false_interruption"
    CALL_TRANSPORT_FAILURE = "call_transport_failure"
    INVALID_EVENT_ORDER = "invalid_event_order"
    INSTRUMENTATION_FAILURE = "instrumentation_failure"
    CONFIGURATION_MISMATCH = "configuration_mismatch"


INELIGIBLE_FAILURES = frozenset(
    {
        FailureCode.AGENT_NEVER_SPOKE,
        FailureCode.STIMULUS_STARTED_WITHOUT_AGENT_AUDIO,
        FailureCode.STIMULUS_DELIVERY_FAILED,
        FailureCode.MEDIA_CAPTURE_MISSING,
        FailureCode.INVALID_EVENT_ORDER,
        FailureCode.INSTRUMENTATION_FAILURE,
        FailureCode.CONFIGURATION_MISMATCH,
    }
)


@dataclass(frozen=True, slots=True)
class TrialEvidence:
    """Condition-independent facts used for immutable trial classification."""

    agent_audio_active: bool
    stimulus_started: bool
    stimulus_delivered: bool
    media_capture_present: bool
    configuration_matches: bool
    event_order_valid: bool
    instrumentation_complete: bool
    transport_complete: bool
    eligible_trigger_count: int
    interrupt_action_count: int
    acoustic_stop_found: bool
    stop_before_natural_end: bool
    caller_turn_count: int
    stale_audio_resumed: bool
    next_response_based_on_turn: bool
    ended_naturally: bool = False
    stimulus_required: bool = True


@dataclass(frozen=True, slots=True)
class TrialClassification:
    eligible: bool
    successful: bool
    failure_codes: tuple[str, ...]


def classify_trial(evidence: TrialEvidence) -> TrialClassification:
    """Apply plan sections 10-11 without consulting observed latency."""
    failures: set[FailureCode] = set()
    if not evidence.agent_audio_active:
        failures.add(FailureCode.AGENT_NEVER_SPOKE)
        if evidence.stimulus_started:
            failures.add(FailureCode.STIMULUS_STARTED_WITHOUT_AGENT_AUDIO)
    if evidence.stimulus_required and (
        not evidence.stimulus_started or not evidence.stimulus_delivered
    ):
        failures.add(FailureCode.STIMULUS_DELIVERY_FAILED)
    if not evidence.media_capture_present:
        failures.add(FailureCode.MEDIA_CAPTURE_MISSING)
    if not evidence.configuration_matches:
        failures.add(FailureCode.CONFIGURATION_MISMATCH)
    if not evidence.event_order_valid:
        failures.add(FailureCode.INVALID_EVENT_ORDER)
    if not evidence.instrumentation_complete:
        failures.add(FailureCode.INSTRUMENTATION_FAILURE)
    if not evidence.transport_complete:
        failures.add(FailureCode.CALL_TRANSPORT_FAILURE)

    if evidence.stimulus_required and evidence.eligible_trigger_count == 0:
        failures.add(FailureCode.TRIGGER_NOT_OBSERVED)
    if evidence.eligible_trigger_count > 0 and evidence.interrupt_action_count == 0:
        failures.add(FailureCode.INTERRUPT_ACTION_NOT_OBSERVED)
    if evidence.interrupt_action_count > 1:
        failures.add(FailureCode.DUPLICATE_INTERRUPT_ACTION)
    if not evidence.stimulus_required and evidence.interrupt_action_count:
        failures.add(FailureCode.FALSE_INTERRUPTION)
    if evidence.ended_naturally or (
        evidence.acoustic_stop_found and not evidence.stop_before_natural_end
    ):
        failures.add(FailureCode.NATURAL_UTTERANCE_END)
    if not evidence.acoustic_stop_found:
        failures.add(FailureCode.ACOUSTIC_STOP_NOT_FOUND)
    if evidence.caller_turn_count == 0 and evidence.stimulus_required:
        failures.add(FailureCode.CALLER_TURN_LOST)
    if evidence.caller_turn_count > 1:
        failures.add(FailureCode.CALLER_TURN_DUPLICATED)
    if evidence.stale_audio_resumed:
        failures.add(FailureCode.STALE_AUDIO_RESUMED)

    eligible = not bool(failures & INELIGIBLE_FAILURES)
    successful = (
        eligible
        and evidence.agent_audio_active
        and evidence.eligible_trigger_count == 1
        and evidence.interrupt_action_count == 1
        and evidence.acoustic_stop_found
        and evidence.stop_before_natural_end
        and evidence.caller_turn_count == 1
        and not evidence.stale_audio_resumed
        and evidence.next_response_based_on_turn
        and not failures
    )
    return TrialClassification(
        eligible=eligible,
        successful=successful,
        failure_codes=tuple(sorted(code.value for code in failures)),
    )


def balanced_condition_order(
    *, seed: int, attempts_per_condition: int, block_per_condition: int = 2
) -> tuple[BenchmarkMode, ...]:
    """Generate deterministic, balanced interleaved blocks."""
    if attempts_per_condition <= 0:
        raise ValueError("attempts_per_condition must be positive")
    if block_per_condition <= 0 or attempts_per_condition % block_per_condition:
        raise ValueError("attempt count must be divisible by block_per_condition")
    rng = random.Random(seed)
    order: list[BenchmarkMode] = []
    for _ in range(attempts_per_condition // block_per_condition):
        block = [
            condition
            for condition in PRIMARY_CONDITIONS
            for _ in range(block_per_condition)
        ]
        rng.shuffle(block)
        order.extend(block)
    return tuple(order)


def _git_provenance(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    clean = not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return commit, clean


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    *,
    stage: Literal["qualification", "final"],
    seed: int,
    attempts_per_condition: int,
    profile_path: Path,
    repo_root: Path,
    require_clean: bool = True,
) -> dict[str, object]:
    """Create a prospective manifest; a dirty formal revision fails closed."""
    commit, clean = _git_provenance(repo_root)
    if require_clean and not clean:
        raise ValueError("formal Phase 3 manifests require a clean git tree")
    order = balanced_condition_order(
        seed=seed, attempts_per_condition=attempts_per_condition
    )
    prefix = "p3q" if stage == "qualification" else "p3f"
    trials = [
        {
            "attempt_index": index,
            "trial_id": f"{prefix}-{index:03d}",
            "condition": condition.value,
            "status": "scheduled",
        }
        for index, condition in enumerate(order, start=1)
    ]
    try:
        recorded_profile_path = str(profile_path.relative_to(repo_root))
    except ValueError:
        recorded_profile_path = profile_path.name
    return {
        "schema_version": 1,
        "stage": stage,
        "created_utc": datetime.now(UTC).isoformat(),
        "ordering": {
            "algorithm": "balanced-block-shuffle-v1",
            "seed": seed,
            "block_per_condition": DEFAULT_ORDER_BLOCK_PER_CONDITION,
            "attempts_per_condition": attempts_per_condition,
            "failed_attempts_replaced": False,
        },
        "git": {"commit": commit, "clean": clean},
        "measurement_profile": {
            "path": recorded_profile_path,
            "sha256": _sha256_file(profile_path),
        },
        "matched_difference": "trigger eligibility only",
        "qualification_protocol": {
            "harness_mode": "phase3",
            "barge_offset_ms": PHASE3_BARGE_OFFSET_MS,
            "natural_end_reference_ms": PHASE3_NATURAL_END_MS,
            "natural_end_reference_run_id": PHASE3_NATURAL_END_RUN_ID,
            "natural_end_derivation": (
                "frozen detector reanalysis: samples 104960..156160 at 16 kHz"
            ),
        },
        "trials": trials,
    }


def write_new_json(path: Path, value: object) -> None:
    """Write once with owner-only permissions; refuse to overwrite evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
        handle.write(payload)
        handle.flush()


def write_trial_record(
    path: Path,
    *,
    trial_id: str,
    condition: BenchmarkMode,
    evidence: TrialEvidence,
) -> None:
    classification = classify_trial(evidence)
    write_new_json(
        path,
        {
            "schema_version": 1,
            "trial_id": trial_id,
            "condition": condition.value,
            "evidence": asdict(evidence),
            "classification": asdict(classification),
        },
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a Phase 3 run manifest")
    parser.add_argument("--stage", choices=("qualification", "final"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--attempts-per-condition", type=int)
    parser.add_argument(
        "--profile", type=Path, default=Path("bench/measurement_profile.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    default_count = (
        QUALIFICATION_ATTEMPTS_PER_CONDITION
        if args.stage == "qualification"
        else FINAL_ATTEMPTS_PER_CONDITION
    )
    manifest = build_manifest(
        stage=args.stage,
        seed=args.seed,
        attempts_per_condition=args.attempts_per_condition or default_count,
        profile_path=args.profile.resolve(),
        repo_root=Path.cwd(),
        require_clean=not args.allow_dirty,
    )
    write_new_json(args.output, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
