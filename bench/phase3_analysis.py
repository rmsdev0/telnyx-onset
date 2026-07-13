"""Generate the preregistered Phase 3 aggregate analysis from raw trials."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from bench.phase3 import write_new_json

ANALYSIS_SEED = 2026071305
BOOTSTRAP_RESAMPLES = 10_000


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(label)
    return value


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires observations")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "median": statistics.median(values),
        "q1": _percentile(values, 0.25),
        "q3": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "minimum": min(values),
        "maximum": max(values),
    }


def _bootstrap_median_ci(
    values: list[float], *, seed: int, resamples: int
) -> list[float] | None:
    if not values:
        return None
    generator = random.Random(seed)
    medians = [
        statistics.median(generator.choices(values, k=len(values)))
        for _ in range(resamples)
    ]
    return [_percentile(medians, 0.025), _percentile(medians, 0.975)]


def _load_events(path: Path) -> list[dict[str, Any]]:
    return [
        _object(json.loads(line), "agent event must be an object")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _first_event(
    events: list[dict[str, Any]], name: str, **required: object
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in events
            if row.get("event") == name
            and all(row.get(key) == value for key, value in required.items())
        ),
        None,
    )


def _agent_decomposition(
    events: list[dict[str, Any]], condition: str
) -> dict[str, float]:
    if condition == "onset-fd-vad":
        trigger = _first_event(events, "vad_decision", eligible_for_interruption=True)
    else:
        trigger = next(
            (
                row
                for row in events
                if row.get("event")
                in {"first_transcript_interim", "first_transcript_final"}
                and row.get("eligible_for_interruption") is True
                and row.get("non_empty") is True
            ),
            None,
        )
    request = _first_event(events, "interruption_requested")
    invalidated = _first_event(events, "local_media_epoch_invalidated")
    clear_start = _first_event(events, "clear_send_started")
    clear_end = _first_event(events, "clear_send_finished", outcome="completed")
    rows = [trigger, request, invalidated, clear_start, clear_end]
    if any(row is None for row in rows):
        raise ValueError("successful trial lacks decomposition milestone")
    complete = [row for row in rows if row is not None]
    clock_ids = {row.get("clock_id") for row in complete}
    if len(clock_ids) != 1 or None in clock_ids:
        raise ValueError("agent decomposition clock mismatch")
    stamps = [row.get("monotonic_ns") for row in complete]
    if not all(isinstance(value, int) for value in stamps):
        raise ValueError("agent decomposition timestamp missing")
    trigger_ns, request_ns, invalidated_ns, clear_start_ns, clear_end_ns = stamps
    assert isinstance(trigger_ns, int)
    assert isinstance(request_ns, int)
    assert isinstance(invalidated_ns, int)
    assert isinstance(clear_start_ns, int)
    assert isinstance(clear_end_ns, int)
    return {
        "trigger_to_request_ms": (request_ns - trigger_ns) / 1_000_000,
        "request_to_epoch_invalidation_ms": (invalidated_ns - request_ns) / 1_000_000,
        "request_to_clear_start_ms": (clear_start_ns - request_ns) / 1_000_000,
        "clear_send_duration_ms": (clear_end_ns - clear_start_ns) / 1_000_000,
    }


def analyze_session(
    session: dict[str, Any],
    *,
    artifact_root: Path,
    seed: int = ANALYSIS_SEED,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, object]:
    raw_trials = session.get("trials")
    if not isinstance(raw_trials, list) or not raw_trials:
        raise ValueError("session trials missing")
    trials = [_object(value, "trial must be an object") for value in raw_trials]
    conditions = ("onset-fd-vad", "onset-fd-transcript")
    summaries: dict[str, object] = {}
    medians: dict[str, float] = {}
    for condition_index, condition in enumerate(conditions):
        attempted = [row for row in trials if row.get("condition") == condition]
        eligible = [
            row
            for row in attempted
            if _object(row.get("classification"), "classification missing").get(
                "eligible"
            )
            is True
        ]
        successful = [
            row
            for row in eligible
            if _object(row.get("classification"), "classification missing").get(
                "successful"
            )
            is True
        ]
        failures = Counter(
            str(code)
            for row in attempted
            for code in _object(
                row.get("classification"), "classification missing"
            ).get("failure_codes", [])
        )
        latencies = [
            float(
                _object(row.get("metrics"), "metrics missing")[
                    "harness_boundary_latency_ms"
                ]
            )
            for row in successful
        ]
        latency_summary = _summary(latencies)
        if latency_summary is not None:
            medians[condition] = float(latency_summary["median"])
        decomposition: dict[str, list[float]] = {}
        for row in successful:
            run_id = str(row.get("harness_run_id"))
            values = _agent_decomposition(
                _load_events(artifact_root / run_id / "agent_events.jsonl"),
                condition,
            )
            for key, value in values.items():
                decomposition.setdefault(key, []).append(value)
        summaries[condition] = {
            "attempted": len(attempted),
            "eligible": len(eligible),
            "successful": len(successful),
            "success_rate_per_attempt": len(successful) / len(attempted),
            "success_rate_per_eligible": (
                len(successful) / len(eligible) if eligible else None
            ),
            "failure_counts": dict(sorted(failures.items())),
            "harness_boundary_latency_ms": latency_summary,
            "median_bootstrap_95_ci_ms": _bootstrap_median_ci(
                latencies,
                seed=seed + condition_index,
                resamples=bootstrap_resamples,
            ),
            "agent_local_decomposition_ms": {
                key: _summary(values) for key, values in sorted(decomposition.items())
            },
        }
    comparison = None
    if set(medians) == set(conditions):
        comparison = {
            "design": "unpaired independent trials",
            "direction": "transcript_minus_vad_ms",
            "median_difference_ms": (
                medians["onset-fd-transcript"] - medians["onset-fd-vad"]
            ),
        }
    return {
        "schema_version": 1,
        "analysis_seed": seed,
        "bootstrap_resamples": bootstrap_resamples,
        "qualification_excluded": session.get("manifest", "").find("qualification")
        >= 0,
        "conditions": summaries,
        "comparison": comparison,
        "cross_process_subtractions": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    session = _object(json.loads(arguments.session.read_text()), "session object")
    write_new_json(
        arguments.output,
        analyze_session(session, artifact_root=arguments.artifact_root),
    )


if __name__ == "__main__":
    main()
