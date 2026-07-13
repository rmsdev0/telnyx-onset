# Phase 3 execution report

**Status:** formal live qualification passed after two documented detector
recalibrations; final 40+40 collection is ready to freeze and execute.

Strict runtime revision: `ee99b481860e452f2d89a189d6e15194d3afcc2e`.
Qualification runtime revision: `1e77224679dc0be74e9b69b44109087fd3b00ccf`.

## Frozen inputs

- `bench/measurement_profile.json` is authoritative and refrozen at a 600 ms
  sustained-silence hold after the dated recalibration reviews.
- Phase 2 verdict remains `GO`.
- The primary matched conditions are `onset-fd-vad` and
  `onset-fd-transcript`; their only allowed difference is trigger eligibility.

## Runtime implementation

- Strict VAD mode allows only a VAD decision to request interruption.
- Strict transcript mode records VAD decisions but allows only the first
  non-empty interim/final transcript to request interruption.
- The transcript event that requests interruption continues through turn
  assembly; response teardown no longer clears an in-progress caller turn.
- One response generation owns LLM/TTS work, media epoch, interruption,
  cancellation, mark completion, and teardown.
- Media clear returns epoch invalidation and clear start/completion/failure
  facts instead of suppressing the outcome.
- Sanitized agent-local JSONL includes clock identity, frozen-profile hash,
  matched-config hash, trigger observations, interruption/clear/cancellation,
  exactly-once turn completion, and next-response ownership. It contains no
  transcript text, audio, phone number, call-control id, or secret.
- Strict startup rejects duplex-policy mismatches, missing benchmark metadata,
  unsafe trial ids, an existing event path, a non-frozen profile, and runtime
  audio settings that differ from the frozen profile.

## Trial infrastructure

- Deterministic balanced-block ordering records its seed and preserves the
  declared 10+10 qualification and 40+40 final attempted-trial policies.
- Manifests and trial classifications are owner-only, write-once files.
- Classification records overlapping failure codes, separates eligibility
  from success, and never uses observed latency to determine eligibility.
- A formal manifest requires a clean git revision. Qualification and final
  populations remain separate.

## Verification gate

Offline tests exercise both strict policies, ineligible-source inertness,
exactly-once caller-turn preservation, one cancellation and clear action per
generation, clear failure visibility, frozen-profile validation, deterministic
ordering, overlapping failure classification, and write-once evidence.

The complete repository gate passes with 233 tests, Ruff, and strict mypy over
`onset/`, `bench/`, and `tests/`.

## Prospective qualification order

The first seed-`20260713` manifest was superseded before any call after a
preflight audit found that the Phase 2 SIP measurement mode emits its fixture
after natural greeting stop. No qualification evidence was collected with that
invalid path. The corrected `phase3` harness mode schedules the fixture 1,000 ms
after the frozen detector confirms active playback, measures the subsequent
acoustic stop, and records whether it precedes the frozen natural end.

The natural-end reference is 3,200 ms from active onset: frozen-profile
reanalysis of agent-only capture `p2-1de191a7ca4e9f33`, samples
104,960–156,160 at 16 kHz. A replacement prospective manifest will be generated
from the clean corrected revision before any provider call. Failed attempts
will not be replaced, and qualification remains excluded from final analysis.

The replacement `bench/phase3_qualification_manifest.json` records clean
revision `1e77224`, the unchanged seed/order, the exact profile hash, the
1,000 ms mid-playback offset, and the 3,200 ms natural-end reference.

## Live qualification verdict

The initial 300 ms campaign and the replacement 400 ms campaign were discarded
in full after pooled natural-pause false stops. The unchanged finite calibration
grid and original selection rule advanced the frozen hold first to 400 ms and
then to 600 ms; both dated reviews and evidence hashes are recorded in
`PHASE2_REPORT.md` and `measurement_profile.json`. A preflight-only replacement
manifest created no call and was superseded after test annotation fixes.

The clean 600 ms campaign
`bench/phase3_qualification_manifest_recalibration2.json` then completed all 20
scheduled attempts with no replacements:

- transcript: 10 attempted, 10 eligible, 10 successful;
- VAD: 10 attempted, 8 eligible, 8 successful;
- the two VAD exclusions were the overlapping preregistered categories
  `agent_never_spoke` and `stimulus_started_without_agent_audio` when fixture
  onset landed on an acoustic pause;
- zero stale-audio resumptions, terminal failures, transport/configuration
  faults, duplicate actions, or lost/duplicated caller turns;
- every eligible trial recorded exactly one eligible trigger, interruption
  action, caller turn, and turn-owned next response.

Qualification therefore passes the Section 20 detector, strict-trigger,
generation-ownership, caller-turn, raw-record, and repeatability gates. Its
latencies remain excluded from final analysis.

## Frozen final analysis

`bench.phase3_analysis` freezes analysis seed `2026071305`, 10,000 percentile
bootstrap resamples, linear-interpolated quartiles/p90, and an unpaired
independent-trial comparison. It reports attempted/eligible/successful counts,
overlapping failure counts, success rates, harness-boundary median/IQR/p90 and
median confidence intervals, transcript-minus-VAD median difference, and
agent-local trigger/request/epoch/clear decomposition. It performs no
cross-process timestamp subtraction. Qualification and final session inputs
remain explicitly separated.

The restore-safe live runner launches one fail-closed agent process per trial,
preserves agent/harness evidence together, restores the external webhook in a
`finally` path, and classifies every scheduled attempt without replacement.
