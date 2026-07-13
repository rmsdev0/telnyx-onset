# Phase 3 preparation

**Status:** Phase 3 runtime implementation and offline qualification are
complete. The first strict-mode live qualification campaign was invalidated by
a natural-pause false stop. Recalibration review 1 is complete and
`bench/measurement_profile.json` is refrozen; the complete live qualification
must now be rerun before final collection.

## Where Phase 2 stands (2026-07-13)

- SIP attempt 5 (`p2-96ddebf9e4d6eb36`, revision `1056e28`) produced the
  first `CAPTURE_COMPLETE_PENDING_REVIEW`.
- Manual waveform agreement: **recorded** (PHASE2_REPORT, 2026-07-13),
  including the void-aware checks. A completion audit corrected the review to
  125 active non-void Window-A frames plus 10 voided frames and a 7.44 s
  sustained Window-D onset. Attempt 5 remains diagnostic because its revision
  did not enforce fixture-interval transmit-counter deltas.
- Amendment 1 revision 3 / SIP spec revision 4: in force for calibration
  captures, with delivery enforcement and explicit control modes implemented.
- Calibration controls completed at revision `2afa41d`: agent-only
  `p2-1de191a7ca4e9f33`, no-stimulus `p2-f57fc9336a1784c4`, and echo-control
  `p2-fb90d83bd3e307a2`.
- Bounded calibration completed over seven labels and 1,120 records. The
  initially selected candidate was RMS, 20 ms, −42/−42 dBFS, 100 ms arm,
  300 ms hold.
- The first live qualification campaign invalidated that hold after trial 20
  exposed a 379 ms natural pause followed by resumed old-response audio.
  Recalibration review 1 pooled that condition-independent failure as an
  eighth natural-pause label and repeated the unchanged finite grid (1,280
  records). The same selection rule retains −42/−42 dBFS and selects the
  shortest passing hold, 400 ms. The first qualification campaign is discarded
  in full; a fresh manifest and complete rerun are required.
- Corrected full attempt 6 (`p2-eba91f36fcd7334f`, revision `4227dff`) reached
  `CAPTURE_COMPLETE_PENDING_REVIEW` with enforced delivery, zero voids and RTP
  anomalies, and a passing independent evidence audit.
- The maintainer inspected the annotated waveform, listened to the corrected
  RX/TX tracks, and recorded agreement on 2026-07-13.
- `bench/measurement_profile.json`: frozen; changing any profile value requires
  recalibration before qualification can resume.

## Phase 2 closeout

The final corrected-capture agreement and independent evidence pass are recorded
in `bench/PHASE2_REPORT.md`. The frozen profile contains the selected detector,
SIP topology and codec, fixture hashes, delivery requirements, loss-void bounds,
and sanitized evidence identifiers. No further Phase 2 call is required.

## Completed Phase 2 work

### 1. Calibration captures — complete

Three classes of separately authorized bounded calls, each with the same
teardown-and-evidence discipline as attempts 1–5. All audio is synthetic;
artifacts stay local and ignored.

1. **No-stimulus control** — dial the agent, transmit only the silent
   caller line, never emit the fixture, capture the full call. Purpose:
   false-interruption and contamination baseline; confirms nothing on rx
   correlates with the fixture when none was sent.
2. **Stimulus-echo control** — transmit the fixture against a
   non-responding far end (or during a window where the agent cannot
   speak), and measure any rx return. Purpose: quantify line echo so the
   Window D echo gate's threshold is evidence-based, not assumed. The
   no-stimulus control is structurally blind to this effect.
3. **Agent-only baseline** — capture an uninterrupted scripted agent
   utterance end-to-end. Purpose: the natural-end reference and
   natural-pause segments for detector calibration labels.

Implementation status: the harness exposes `--mode no-stimulus`,
`--mode echo-control`, and `--mode agent-only`. Each is a bench-only,
fail-closed control-loop variant with a named pending-review outcome, never a
measurement run. Echo control retains mandatory fixture-delivery deltas.

### 2. Bounded detector calibration — complete

Run the existing `bench/acoustic_stop.evaluate_bounded_calibration` over
labeled segments cut from the calibration captures plus attempt 5's
captured windows: natural-pause segments (must not trigger a stop) and
forced-stop segments (must trigger). The declared finite candidate set is
already in code (RMS dBFS statistic, 20 ms windows, threshold pairs, holds
100–1000 ms). Record every candidate, pass, and named failure; select
nothing automatically.

### 3. Independent evidence review — complete

An independent review of: the attempt-5 sanitized evidence and manual
agreement, the calibration capture results, the chosen detector candidate,
and the loss-void machinery's behavior across attempts 4–5. Only after its
sign-off, and after a corrected full capture with enforced delivery and manual
waveform agreement, is `measurement_profile.json` created and frozen (with the
detector candidate, capture topology identifiers, fixture hashes, and the
addendum's bounds), and Phase 2 closes as empirically GO-capable.

## Phase 3 execution status

The runtime now exposes fail-closed `onset-fd-vad` and
`onset-fd-transcript` modes, generation-scoped interruption/cancellation/clear
milestones, frozen-profile loading, and sanitized monotonic JSONL records. The
interrupting transcript is preserved for turn assembly rather than discarded.
Offline adversarial tests prove that each ineligible source is inert and that
the caller turn commits once.

`bench.phase3` now prepares write-once, deterministic balanced-block manifests
for qualification/final runs and applies the plan's overlapping failure
taxonomy without replacing failed attempts. Formal qualification still
requires a clean committed revision and separately authorized bounded live
calls through the common SIP boundary.

The initial prospective manifest was superseded before any call because its
preflight exposed a Phase 2/Phase 3 scheduling mismatch. The corrected Phase 3
harness emits 1,000 ms into confirmed active playback and uses the 3,200 ms
frozen natural-end reference derived from the agent-only control. A replacement
manifest must be frozen from the corrected clean revision before live calls.

## Phase 3 scope

Per plan §4–§5 and §20, Phase 3 is benchmark-ready runtime work:

- Implement the **strict matched trigger modes**: `onset-fd-vad` (VAD
  decision is the sole interruption-eligible source) and
  `onset-fd-transcript` (transcripts interrupt; VAD recorded but inert).
  At the inspected revision these do not exist as standalone conditions;
  a verification record must prove the ineligible source cannot interrupt
  in each mode.
- Instrument the plan §8 milestone records (stimulus start, first
  speech-bearing frame, VAD decision, first eligible transcript,
  interruption request/source, media epoch invalidation, clear send,
  teardown, caller-turn completion) on the declared clocks.
- Build trial collection: lifecycle classification, the failure taxonomy,
  eligibility rules, interleaved condition ordering with a recorded seed,
  and the sample plan (40 + 40 primary, diagnostics and controls per plan
  §14) — attempted trials, never retry-until-success.
- Only after qualification runs validate the frozen detector does final
  collection begin.

## Operational notes for the next session

- Live-call procedure (proven in attempts 1–5): ngrok on 8001; production
  server `PORT=8001 MEDIA_STREAM_URL=wss://<tunnel>/ws/media
  TTS_STREAMING_DECODE=false .venv/bin/python -m onset`; agent app webhook
  → `https://<tunnel>/webhook`; harness `BENCH_LIVE=1 .venv/bin/python -m
  bench.sip_harness --live --fixture
  bench/artifacts/fixtures/caller_table_for_two_v1.wav`; teardown restores
  the webhook and stops everything. pjsua2 aborts the process after
  artifacts flush (exit 134) — expected and harmless.
- pjsua2 2.15.1 is built into `.venv` from source (see scratchpad build
  notes); rebuilding requires swig + the pjproject 2.15.1 tree.
- The attempt-5 run directory including `review/` is promotion evidence:
  do not delete it.
