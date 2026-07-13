# Phase 3 preparation

**Status:** preparation only. Phase 3 has NOT begun. Per `BENCHMARK_PLAN.md`
§20 and `bench/PHASE2_REPORT.md`, Phase 3 may not start until every
remaining Phase 2 promotion gate below passes and
`bench/measurement_profile.json` is frozen under independent review.

## Where Phase 2 stands (2026-07-13)

- SIP attempt 5 (`p2-96ddebf9e4d6eb36`, revision `1056e28`) produced the
  first `CAPTURE_COMPLETE_PENDING_REVIEW`.
- Manual waveform agreement: **recorded** (PHASE2_REPORT, 2026-07-13),
  including the void-aware checks.
- `bench/measurement_profile.json`: absent by design, and must remain so
  until the gates below pass.

## Remaining Phase 2 gates, in order

### 1. Calibration captures (plan §7 and §13, under the SIP topology)

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

Implementation note: the harness needs a small `--mode` switch
(no-stimulus / echo-control / agent-only) that disables fixture arming or
the response window as appropriate; each mode is a bench-only control-loop
variant with its own named terminal outcome, never a measurement run.

### 2. Bounded detector calibration (plan §7)

Run the existing `bench/acoustic_stop.evaluate_bounded_calibration` over
labeled segments cut from the calibration captures plus attempt 5's
captured windows: natural-pause segments (must not trigger a stop) and
forced-stop segments (must trigger). The declared finite candidate set is
already in code (RMS dBFS statistic, 20 ms windows, threshold pairs, holds
100–1000 ms). Record every candidate, pass, and named failure; select
nothing automatically.

### 3. Independent evidence review

An independent review of: the attempt-5 sanitized evidence and manual
agreement, the calibration capture results, the chosen detector candidate,
and the loss-void machinery's behavior across attempts 4–5. Only after its
sign-off is `measurement_profile.json` created and frozen (with the
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
