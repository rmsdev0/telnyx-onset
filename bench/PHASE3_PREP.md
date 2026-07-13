# Phase 3 preparation

**Status:** preparation only. Phase 3 has NOT begun. Per `BENCHMARK_PLAN.md`
§20 and `bench/PHASE2_REPORT.md`, Phase 3 may not start until the maintainer
listens to the corrected capture, records agreement, and
`bench/measurement_profile.json` is frozen.

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
  selected candidate is RMS, 20 ms, −42/−42 dBFS, 100 ms arm, 300 ms hold.
- Corrected full attempt 6 (`p2-eba91f36fcd7334f`, revision `4227dff`) reached
  `CAPTURE_COMPLETE_PENDING_REVIEW` with enforced delivery, zero voids and RTP
  anomalies, and a conditionally passing independent evidence audit.
- `bench/measurement_profile.json`: absent pending the maintainer's corrected
  RX/TX listening agreement.

## Remaining Phase 2 gate

Open `bench/artifacts/p2-eba91f36fcd7334f/review/manual_review.html`, inspect
the annotated waveform, and listen to both embedded tracks. Record agreement or
the specific disagreement in `bench/PHASE2_REPORT.md`. On agreement, create the
frozen measurement profile with the selected detector, SIP topology and codec,
fixture hashes, delivery requirements, and loss-void bounds. No new call is
required.

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

### 3. Independent evidence review — conditional pass

An independent review of: the attempt-5 sanitized evidence and manual
agreement, the calibration capture results, the chosen detector candidate,
and the loss-void machinery's behavior across attempts 4–5. Only after its
sign-off, and after a corrected full capture with enforced delivery and manual
waveform agreement, is `measurement_profile.json` created and frozen (with the
detector candidate, capture topology identifiers, fixture hashes, and the
addendum's bounds), and Phase 2 closes as empirically GO-capable.

## What Phase 3 actually is (scope reminder, not a start)

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
