"""Render the publication comparison image for the Phase 3 blog post.

One VAD-triggered trial and one transcript-triggered trial from the frozen
Phase 3 final session, side by side, both rx lanes aligned on the fixture
emission boundary, so the reader sees one agent's audio stop visibly
earlier than the other.

Read-only with respect to session artifacts: this script hashes every file
it touches before and after rendering and fails if anything changed. All
displayed numbers come from the frozen manifests; nothing is recomputed.

The waveform extraction (per-frame min/max bands mapped onto host time via
frame_metadata.jsonl) and the visual system (dark surface, mono labels,
dashed boundary markers) reuse the manual-review tool's machinery
(bench/artifacts/p2-96ddebf9e4d6eb36/review/build_review.py and
gen_review_html.py), recomposed for a static two-lane PNG.

Trial selection (frozen 2026-07-14, see docs/assets/phase3-pair.json):
eligible + successful + no failure codes + zero rx voids, latency nearest
each condition's published median. The strictly nearest pair (p3f-032 +
p3f-071) has a pair difference of 640.199 ms, which rounds to the condition
medians' difference (640.2 ms) and is therefore forbidden as a pair label;
the transcript trial steps to the next-nearest clean candidate p3f-019
(0.2 ms from its median), giving an unambiguous pair delta of 639.9 ms.

Dependencies: stdlib + Pillow. Run with any Python 3.12 environment that
has Pillow; this script deliberately does not import the bench package so
it cannot touch live-harness code paths.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import wave
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
ART = REPO / "bench" / "artifacts"
OUT = REPO / "docs" / "assets"

VAD_RUN = "p2-2ae8c01cc3b72d24"  # p3f-032
TRANSCRIPT_RUN = "p2-ff108cb653122c13"  # p3f-019
MEDIANS_MS = {"onset-fd-vad": 400.7, "onset-fd-transcript": 1040.9}
SUCCESSFUL_TRIALS = 60  # 32 vad + 28 transcript, phase3_final_analysis.json

SURFACE = (20, 20, 19)
PANEL = (32, 32, 31)
INK = (236, 236, 232)
INK2 = (158, 157, 148)
LINE = (58, 57, 54)
GREEN = (74, 222, 128)  # vad lane, #4ade80
BLUE = (96, 165, 250)  # transcript lane, #60a5fa
FONT = "/System/Library/Fonts/Menlo.ttc"

FRAME_NS = 20_000_000  # verification tolerance: one 20 ms frame


@dataclass(frozen=True)
class Trial:
    condition: str
    trial_id: str
    run_id: str
    latency_ms: float
    created_utc: str
    git_commit: str
    env: list[tuple[float, int, int]]  # (seconds rel emission, lo, hi)
    fixture_end_rel_s: float
    stop_rel_s: float


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive_hashes() -> dict[str, str]:
    files: list[Path] = [
        ART / "phase3_final_session.json",
        REPO / "bench" / "phase3_final_manifest.json",
    ]
    for run in (VAD_RUN, TRANSCRIPT_RUN):
        files.extend(sorted((ART / run).iterdir()))
    return {str(f.relative_to(REPO)): _sha256(f) for f in files if f.is_file()}


def _load_trial(run_id: str) -> Trial:
    run = ART / run_id
    manifest = json.loads((run / "manifest.json").read_text())
    p3 = manifest["phase3"]
    events = {
        row["event"]: row
        for row in map(json.loads, (run / "events.jsonl").read_text().splitlines())
        if row["event"] not in ()
    }
    rows = [
        json.loads(line)
        for line in (run / "frame_metadata.jsonl").read_text().splitlines()
    ]
    with wave.open(str(run / "rx_8k.wav")) as handle:
        rx = array("h")
        rx.frombytes(handle.readframes(handle.getnframes()))

    stim_ns = p3["stimulus_start_host_ns"]
    stop_ns = p3["acoustic_stop_host_ns"]
    tx_end_ns = events["stimulus_transmission_completed"]["host_monotonic_ns"]

    # Review-tool mapping: rx frame i <-> metadata row i, one 20 ms frame
    # (160 samples at 8 kHz) per row, timed by its host receive stamp.
    env: list[tuple[float, int, int]] = []
    for i in range(min(len(rx) // 160, len(rows))):
        seg = rx[i * 160 : (i + 1) * 160]
        t = (rows[i]["host_receive_monotonic_ns"] - stim_ns) / 1e9
        env.append((t, min(seg), max(seg)))

    return Trial(
        condition=p3["condition"],
        trial_id=p3["trial_id"],
        run_id=run_id,
        latency_ms=p3["harness_boundary_latency_ms"],
        created_utc=manifest["created_utc"],
        git_commit=manifest["git_commit"],
        env=env,
        fixture_end_rel_s=(tx_end_ns - stim_ns) / 1e9,
        stop_rel_s=(stop_ns - stim_ns) / 1e9,
    )


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size=size, index=0)


DRAWN_TEXT: list[str] = []


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    label: str,
    size: int,
    fill: tuple[int, int, int],
    anchor: str = "la",
) -> None:
    DRAWN_TEXT.append(label)
    draw.text(xy, label, font=_font(size), fill=fill, anchor=anchor)


def render(
    vad: Trial,
    transcript: Trial,
    width: int,
    height: int,
    t0: float,
    t1: float,
    out_path: Path,
) -> dict[str, float]:
    """Draw one canvas; return rendered marker positions for verification."""
    scale = 2  # supersample for clean edges, then downsample
    w, h = width * scale, height * scale
    img = Image.new("RGB", (w, h), SURFACE)
    draw = ImageDraw.Draw(img, "RGBA")

    margin = int(w * 0.045)
    header_h = int(h * 0.115)
    caption_h = int(h * 0.075)
    axis_h = int(h * 0.055)
    gutter = int(h * 0.085)
    lane_h = (h - header_h - caption_h - axis_h - gutter - int(h * 0.06)) // 2
    lane1_y = header_h + int(h * 0.04)
    lane2_y = lane1_y + lane_h + gutter
    plot_x0 = margin
    plot_x1 = w - margin

    def x(t: float) -> float:
        return plot_x0 + (t - t0) / (t1 - t0) * (plot_x1 - plot_x0)

    fs_head = max(int(h * 0.021), 11 * scale)
    fs_label = max(int(h * 0.024), 12 * scale)
    fs_big = max(int(h * 0.030), 14 * scale)
    fs_axis = max(int(h * 0.019), 10 * scale)

    # Header strip: authenticity markers stay visible.
    draw.rectangle([0, 0, w, header_h], fill=PANEL)
    draw.line([0, header_h, w, header_h], fill=LINE, width=scale)
    _text(
        draw,
        (margin, header_h * 0.22),
        "ONE AGENT, TWO INTERRUPTION TRIGGERS · PHASE 3 FINAL SESSION"
        " · RECEIVED AGENT AUDIO, ALIGNED ON FIXTURE EMISSION",
        fs_head,
        INK,
        anchor="lm",
    )
    _text(
        draw,
        (margin, header_h * 0.58),
        f"vad trial {vad.trial_id} · run {vad.run_id}"
        f" · captured {vad.created_utc[:19]}Z",
        fs_head,
        INK2,
        anchor="lm",
    )
    _text(
        draw,
        (margin, header_h * 0.84),
        f"transcript trial {transcript.trial_id} · run {transcript.run_id}"
        f" · captured {transcript.created_utc[:19]}Z"
        f" · runtime commit {vad.git_commit[:9]}",
        fs_head,
        INK2,
        anchor="lm",
    )

    lanes = (
        (vad, lane1_y, GREEN, "onset-fd-vad"),
        (transcript, lane2_y, BLUE, "onset-fd-transcript"),
    )

    # Fixture-duration shading, subordinate to the rx lanes.
    for trial, y0, _color, _name in lanes:
        draw.rectangle(
            [x(0.0), y0, x(min(trial.fixture_end_rel_s, t1)), y0 + lane_h],
            fill=(255, 255, 255, 14),
        )
    _text(
        draw,
        (x(min(vad.fixture_end_rel_s, t1) / 2), lane1_y - int(h * 0.008)),
        "fixture duration (tx)",
        fs_axis,
        INK2,
        anchor="mb",
    )

    # Waveform bands: min/max polygon per lane, review-tool style.
    for trial, y0, color, name in lanes:
        mid = y0 + lane_h / 2
        amp = lane_h / 2 * 0.92
        pts_top: list[tuple[float, float]] = []
        pts_bot: list[tuple[float, float]] = []
        for t, lo, hi in trial.env:
            if not t0 <= t <= t1:
                continue
            px = x(t)
            pts_top.append((px, mid - hi / 32768 * amp))
            pts_bot.append((px, mid - lo / 32768 * amp))
        draw.line([plot_x0, mid, plot_x1, mid], fill=LINE, width=scale)
        if pts_top:
            draw.polygon(pts_top + list(reversed(pts_bot)), fill=color)
        _text(
            draw,
            (plot_x0, y0 - int(h * 0.008)),
            name,
            fs_label,
            color,
            anchor="lb",
        )

    # Shared t = 0 line across both lanes.
    zero_x = x(0.0)
    top_y = lane1_y - int(h * 0.004)
    bot_y = lane2_y + lane_h + int(h * 0.004)
    yy = top_y
    while yy < bot_y:
        yy_end = min(yy + 6 * scale, bot_y)
        draw.line([zero_x, yy, zero_x, yy_end], fill=INK, width=scale)
        yy += 12 * scale
    _text(
        draw,
        (zero_x + 8 * scale, lane1_y + int(lane_h * 0.06)),
        "caller starts speaking (fixture emission)",
        fs_label,
        INK,
        anchor="la",
    )

    # Per-lane acoustic stop markers, labeled from the manifests.
    rendered: dict[str, float] = {}
    for trial, y0, color, _name in lanes:
        stop_x = x(trial.stop_rel_s)
        rendered[trial.trial_id] = stop_x
        draw.line(
            [stop_x, y0 - int(h * 0.004), stop_x, y0 + lane_h + int(h * 0.004)],
            fill=color,
            width=2 * scale,
        )
        _text(
            draw,
            (stop_x + 8 * scale, y0 + int(lane_h * 0.06)),
            f"stopped at {trial.latency_ms:.1f} ms",
            fs_big,
            color,
            anchor="la",
        )

    # Bracket between the two stop boundaries, in the inter-lane gutter.
    bx0, bx1 = x(vad.stop_rel_s), x(transcript.stop_rel_s)
    by = lane2_y - gutter / 2
    tick = int(h * 0.012)
    draw.line([bx0, by, bx1, by], fill=INK, width=scale)
    draw.line([bx0, by - tick, bx0, by + tick], fill=INK, width=scale)
    draw.line([bx1, by - tick, bx1, by + tick], fill=INK, width=scale)
    pair_delta = transcript.latency_ms - vad.latency_ms
    _text(
        draw,
        ((bx0 + bx1) / 2, by - int(h * 0.010)),
        f"this pair: {pair_delta:.1f} ms",
        fs_big,
        INK,
        anchor="mb",
    )

    # Time axis under the lower lane.
    axis_y = lane2_y + lane_h + int(h * 0.012)
    step = 0.5
    t = t0 + (-t0) % step - step  # first tick at a multiple of step
    while t <= t1 + 1e-9:
        if t0 <= t <= t1:
            draw.line(
                [x(t), axis_y, x(t), axis_y + int(h * 0.008)], fill=INK2, width=scale
            )
            _text(
                draw,
                (x(t), axis_y + int(h * 0.012)),
                f"{t:+.1f} s" if abs(t) > 1e-9 else "0",
                fs_axis,
                INK2,
                anchor="ma",
            )
        t += step

    # Caption, inside the image, bottom.
    _text(
        draw,
        (w / 2, h - caption_h * 0.55),
        "Trials nearest each condition median. Condition medians differ by"
        f" 640.2 ms across {SUCCESSFUL_TRIALS} successful trials.",
        fs_label,
        INK2,
        anchor="mm",
    )

    img = img.resize((width, height), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)

    # Return px-per-second so the caller can back-map marker positions.
    rendered["_px_per_s"] = (plot_x1 - plot_x0) / (t1 - t0) / scale
    rendered["_x0_px"] = plot_x0 / scale
    rendered["_t0"] = t0
    return rendered


def main() -> None:
    before = _archive_hashes()

    vad = _load_trial(VAD_RUN)
    transcript = _load_trial(TRANSCRIPT_RUN)
    assert vad.condition == "onset-fd-vad"
    assert transcript.condition == "onset-fd-transcript"

    print("selected trials:")
    for trial in (vad, transcript):
        median = MEDIANS_MS[trial.condition]
        print(
            f"  {trial.condition}: {trial.trial_id} ({trial.run_id})"
            f" latency {trial.latency_ms:.6f} ms"
            f" delta from median {trial.latency_ms - median:+.3f} ms"
        )
    pair_delta = transcript.latency_ms - vad.latency_ms
    print(f"  pair delta: {pair_delta:.6f} ms (label: {pair_delta:.1f} ms)")

    # Shared window: 2 s pre-roll, transcript stop + about 1 s post-roll.
    t0_full, t1_full = -2.0, transcript.stop_rel_s + 1.0
    # OG tightens pre/post-roll so both stops and the bracket stay legible.
    t0_og, t1_og = -0.8, transcript.stop_rel_s + 0.55

    outputs = [
        ("phase3-pair-blog.png", 2560, 1440, t0_full, t1_full),
        ("phase3-pair-linkedin.png", 1200, 627, t0_full, t1_full),
        ("phase3-pair-og.png", 1200, 630, t0_og, t1_og),
    ]
    checks: list[str] = []
    for name, width, height, t0, t1 in outputs:
        rendered = render(vad, transcript, width, height, t0, t1, OUT / name)
        for trial in (vad, transcript):
            px = rendered[trial.trial_id] / 2  # supersample factor
            back_t = rendered["_t0"] + (px - rendered["_x0_px"]) / rendered["_px_per_s"]
            err_ns = abs(back_t - trial.stop_rel_s) * 1e9
            ok = err_ns <= FRAME_NS
            checks.append(
                f"{name} {trial.trial_id}: marker back-maps to"
                f" {back_t * 1e3:.2f} ms vs manifest"
                f" {trial.stop_rel_s * 1e3:.2f} ms"
                f" (err {err_ns / 1e6:.3f} ms) -> {'OK' if ok else 'FAIL'}"
            )
            if not ok:
                raise SystemExit(f"marker position check failed: {checks[-1]}")
        print(f"wrote {OUT / name} ({width}x{height})")

    print("\nmarker position checks (tolerance 20 ms):")
    for line in checks:
        print(" ", line)

    print("\nlatency label checks:")
    for trial in (vad, transcript):
        label = f"stopped at {trial.latency_ms:.1f} ms"
        print(
            f"  manifest {trial.latency_ms!r} -> label {label!r}"
            f" -> {'OK' if f'{trial.latency_ms:.1f}' in label else 'FAIL'}"
        )

    all_text = "\n".join(DRAWN_TEXT)
    em_dash = "—" in all_text or "–" in all_text
    ids = {vad.trial_id, transcript.trial_id, vad.run_id, transcript.run_id}
    stripped = all_text
    for known in ids | {vad.git_commit[:9]}:
        stripped = stripped.replace(known, "")
    suspicious = re.findall(r"\+?\d{7,}", stripped)
    lowered = stripped.lower()
    leaks = [
        marker
        for marker in ("telnyx", "sip.", "@", "bench_", "password")
        if marker in lowered
    ]
    print("\nrendered text scan:")
    print(f"  em/en dashes: {'FOUND' if em_dash else 'none'}")
    print(f"  long digit runs beyond ids: {suspicious or 'none'}")
    print(f"  identifier markers: {leaks or 'none'}")
    if em_dash or suspicious or leaks:
        raise SystemExit("rendered text scan failed")

    after = _archive_hashes()
    unchanged = before == after
    print(f"\nsession archive hash check: {len(before)} files,"
          f" {'UNCHANGED' if unchanged else 'MODIFIED'}")
    if not unchanged:
        raise SystemExit("session artifacts changed during rendering")

    renderer_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    ).stdout.strip()
    sidecar = {
        "purpose": "provenance for docs/assets/phase3-pair-*.png",
        "created_utc": datetime.now(UTC).isoformat(),
        "trials": {
            trial.condition: {
                "trial_id": trial.trial_id,
                "run_id": trial.run_id,
                "harness_boundary_latency_ms": trial.latency_ms,
                "condition_median_ms": MEDIANS_MS[trial.condition],
                "captured_utc": trial.created_utc,
            }
            for trial in (vad, transcript)
        },
        "pair_delta_ms": pair_delta,
        "pair_delta_label": f"{pair_delta:.1f} ms",
        "condition_median_difference_ms": 640.2,
        "successful_trials": SUCCESSFUL_TRIALS,
        "selection_note": (
            "Nearest-median clean trials; the strictly nearest transcript"
            " trial p3f-071 was passed over because that pair's difference"
            " (640.199 ms) rounds to the condition medians' difference and"
            " the pair label must not read 640.2 ms."
        ),
        "source_manifest_sha256": _sha256(
            REPO / "bench" / "phase3_final_manifest.json"
        ),
        "final_session_sha256": _sha256(ART / "phase3_final_session.json"),
        "runtime_commit": vad.git_commit,
        "renderer_commit": renderer_commit,
    }
    sidecar_path = OUT / "phase3-pair.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
    print(f"wrote {sidecar_path}")


if __name__ == "__main__":
    main()
