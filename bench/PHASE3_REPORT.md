# Phase 3 execution report

**Status:** runtime implemented, offline-qualified, and prospectively ordered;
formal live qualification not started.

Runtime revision: `ee99b481860e452f2d89a189d6e15194d3afcc2e`.

## Frozen inputs

- `bench/measurement_profile.json` remains unchanged and authoritative.
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

The complete repository gate passes with 225 tests, Ruff, and strict mypy over
`onset/`, `bench/`, and `tests/`.

## Prospective qualification order

`bench/phase3_qualification_manifest.json` freezes seed `20260713` and 20
attempted trials (10 per primary condition) in balanced blocks. It records the
clean runtime revision and exact measurement-profile hash. Failed attempts will
not be replaced. Qualification evidence remains excluded from final analysis.

No Phase 3 provider call has been placed. The next irreversible/cost-bearing
step is to commit the runtime revision, create the prospective qualification
manifest, then run the bounded interleaved qualification attempts through the
approved SIP harness. Final collection remains prohibited until qualification
passes without profile tuning.
