# Voice-agent interruption benchmark pre-registration

## 1. Status and provenance

This document pre-registers the benchmark methodology before the full-duplex
implementation is considered benchmark-ready. No final comparative data has
been collected under this protocol. It is a methodology document, not a result
report, a frozen benchmark release, or evidence that the required measurement
capabilities already exist.

| Item | Value |
|---|---|
| Pre-registration date | 2026-07-10 |
| `telnyx-onset` inspected revision | `645a51c20199a870fd19b416f96f36df07a6c825` (`duplex`) |
| `telnyx-offleash` inspected revision | `d1fe627777a658d6d4a6859d5b8624c4ea970d2a` (`main`) |
| Final data under this protocol | None |
| Common harness-side acoustic boundary | Unproven; Phase 2 hard gate |

The revisions above identify the code inspected while writing the plan. They do
not declare either current working tree to be a frozen benchmark release. Each
formal run must separately record its commit, clean-tree status, runtime, and
non-secret configuration.

### Amendment policy

Any methodological amendment must be dated, state exactly what changed and why,
and remain visible in document history. It must be made before viewing final
comparative results affected by the change and must receive another methodology
review before affected final data are collected. Sample counts, eligibility,
failure handling, detector parameters, and metric definitions may not be changed
in response to an emerging favorable or unfavorable result. If affected data
have already been captured, an amendment invalidates those qualification or
final runs; affected conditions restart under the amended, frozen method, and
superseded trials are retained for audit but never pooled with replacement data.

## 2. Decision summary

The study separates three questions that require different claims and outcome
families.

| Question | Comparison | Interpretation |
|---|---|---|
| Observation boundary | `onset-fd-vad` vs `onset-fd-transcript` | Primary matched causal comparison inside `telnyx-onset` |
| Whole-system architecture | `onset-fd-vad` vs `offleash-transcript` | Secondary end-to-end comparison with multiple architectural differences |
| Duplex listening policy | `onset-half-duplex` diagnostic scenarios | Suppression, clipping, and floor-acquisition diagnostic |

The primary causal comparison selects a VAD decision rather than transcript
arrival as the interruption trigger while holding the onset media, STT, TTS,
playback queue, and clear path constant. The offleash comparison is not a clean
VAD-versus-transcript experiment. Half duplex cannot observe a true
mid-playback interruption while its listening gate is closed, so it has no
mid-playback barge-in latency distribution.

## 3. Research questions and hypotheses

### Primary question

Holding the media, STT, TTS, playback, clear operation, and controlled stimulus
constant, how does a local VAD trigger change interruption latency and
interruption correctness relative to a transcript trigger?

**Directional hypothesis:** `onset-fd-vad` will have lower harness-boundary
interruption latency than `onset-fd-transcript`.

This hypothesis is falsifiable. No reduction, a reversal, or materially worse
correctness in the VAD condition is publishable and will not be reclassified or
hidden after collection. A lower latency with greater caller-turn loss,
duplication, false interruption, or stale-audio resumption is not an unqualified
win.

### Secondary question

How does the complete frame-owned `telnyx-onset` implementation compare with the
prior transcript and Call Control `telnyx-offleash` implementation under the
declared benchmark?

This comparison changes media ownership, transcription surface, TTS path, stop
primitive, provider events, and timing boundaries. It cannot attribute the
entire difference solely to VAD or observation timing.

### Diagnostic question

What caller behavior does half-duplex gating intentionally suppress or clip,
and what implementation or correctness burden does that policy avoid?

This is not a question about how slowly half duplex performs barge-in. During
the closed gate, it cannot perform true barge-in at all.

## 4. Benchmark modes

These are benchmark condition names. They are specifications for later phases,
not claims that corresponding runtime switches already exist. At the inspected
`telnyx-onset` revision, `HALF_DUPLEX=false` enables a combined VAD-or-transcript
interrupt path; strict matched trigger modes still have to be implemented and
verified.

| Condition | Required behavior | Current status at inspected revision |
|---|---|---|
| `onset-fd-vad` | Inbound caller media remains available while the agent speaks. The local VAD decision is the sole interruption-eligible source. Transcripts remain necessary for caller-turn assembly but never initiate interruption in the strict matched mode, including as a fallback or second action. Playback stops through the `telnyx-onset` media path. | Not available as a strict standalone condition |
| `onset-fd-transcript` | Uses the same onset transport, inbound media, STT stream, TTS path, playback queue, media epochs, and clear operation. VAD events may be recorded but cannot interrupt. The first eligible non-empty interim or final transcript initiates interruption. | Not available as a strict standalone condition; this is the matched control |
| `offleash-transcript` | Uses the existing Call Control transcription and `playback_stop` architecture. Its historical benchmark remains historical unless the condition is rerun through the approved common boundary. | Runtime architecture exists; current checkout does not wire the historical collection controller and metric sink into its CLI/server |
| `onset-half-duplex` | Caller audio is deliberately replaced with silence while the software speaking gate is active—from entry into `_speak`, including pre-playback synthesis and prebuffering, through completion or timeout—and during the configured guard. Actual acoustic playback is measured separately. It is tested with suppression and floor-acquisition scenarios, not barge-in latency. | Existing `HALF_DUPLEX=true` behavior, subject to benchmark instrumentation and correctness qualification |

## 5. Comparison hierarchy and allowable claims

| Comparison | Held constant | Different | Question answered | Allowed conclusion | Explicitly prohibited |
|---|---|---|---|---|---|
| `onset-fd-vad` vs `onset-fd-transcript` | Onset revision; call/media topology; inbound frames; STT connection and settings; TTS; playback queue and pacing; media epoch and clear path; stimulus; agent fixture; detector and analysis | Trigger eligibility only: VAD decision vs first eligible transcript | Effect of choosing an earlier local VAD decision rather than transcript arrival within this onset implementation | Difference attributable to trigger selection within the declared onset conditions, subject to correctness and feasibility gates | Universal claims about all VADs, STT engines, vendors, endpoints, or voice-agent platforms |
| `onset-fd-vad` vs `offleash-transcript` | Controlled task and, only if feasibility passes, harness-side acoustic boundary and trial policy | Media ownership; STT surface; TTS path; queueing; stop primitive; provider events; timing boundaries; implementation | End-to-end behavior of the two declared systems | Whole-system difference under the declared benchmark | Attributing the whole difference solely to VAD, “observation onset,” or one vendor component |
| `onset-half-duplex` vs full-duplex conditions | Scripted task and declared fixtures where feasible | Listening policy, observability during playback, interruption exposure, state burden | Suppression/clipping, floor acquisition, repeat requirements, false-interruption exposure, and operational complexity | Duplex-policy tradeoffs for these implementations | Ranking half duplex in the same mid-playback barge-in latency distribution |

## 6. Terminology

“The caller started talking” is not one directly interchangeable event. A
fixture emission, an arriving media frame, a VAD decision, and a transcript are
different observations at different boundaries.

| Term | Definition in this benchmark |
|---|---|
| Synthetic stimulus | A versioned, hashed, non-personal audio fixture representing caller speech, emitted by the controlled harness according to the trial script. |
| Stimulus start | The first non-silent sample of that fixture emitted at the controlled harness boundary, timestamped on the harness monotonic clock. It is not the time a remote speak command was requested. |
| First speech-bearing application frame | The earliest inbound application frame satisfying the frozen speech-bearing rule after stimulus start. It is an application-boundary observation, not physical mouth onset. |
| VAD decision | The application event produced when the frozen VAD implementation and accumulation rule cross their configured speech threshold. Use “VAD decision,” never “VAD onset.” |
| First eligible transcript | The first non-empty interim or final transcript after stimulus start that satisfies the condition’s frozen eligibility rule. |
| Caller turn | Exactly one completed user utterance assembled from eligible transcript events and committed as one user turn. |
| Response generation | One generation-scoped unit of LLM/tool work, TTS generation, media enqueueing, playback state, cancellation, and teardown. |
| Interruption request | The generation-scoped application decision to interrupt, recorded before media invalidation or provider stop I/O. |
| Local media invalidation | Invalidation of locally queued and late producer frames, such as an onset media-epoch increment. |
| Provider stop or clear action | A `clear` or `playback_stop` send attempt and its observed completion or failure. It is not proof that caller-side audio is absent. |
| Harness-side acoustic stop | The first sample boundary after which captured agent audio is continuously absent according to the frozen sustained-silence detector. This term is used only when an actual returned waveform is measured. |
| Human-perceived stop | A stop boundary judged by a defined human perception protocol. This study does not measure it. |

“Perceived latency” is prohibited unless a separate human perception study is
performed. Provider events, mark echoes, clear completion, and
`call.speak.ended` must be named as such rather than relabeled as acoustic or
human-perceived stop.

## 7. Primary measurement boundary

The preferred primary endpoint is:

> first non-silent sample of the known synthetic caller stimulus emitted at the
> controlled harness boundary → harness-side agent audio becomes continuously
> absent according to a predeclared sustained-silence detector

The metric name is **harness-boundary interruption latency**.

It is a controlled benchmark-boundary duration intended to put start and end on
one harness monotonic clock. It is not mouth-to-ear latency, human-perceived
latency, or necessarily equivalent to provider-internal media timing.

The common acoustic boundary is not proven by the inspected repositories.
Phase 2 is a hard feasibility and calibration gate. It may not silently replace
the endpoint with `clear` completion, a mark, `call.speak.ended`, or another
weaker proxy. If one harness cannot emit and capture the necessary signals on one
clock, this plan must be amended and re-reviewed before benchmark qualification
or final collection continues.

### Bounded detector and capture calibration

No comparative condition results may be inspected before these choices are
frozen. Calibration uses synthetic, condition-independent fixtures and baseline
captures that are excluded from qualification and final analysis. All selected
values and calibration evidence are recorded with the amendment/review history.

| Unresolved item | Bounded Phase 2 selection rule | Freeze rule |
|---|---|---|
| Audio-energy statistic and threshold | Evaluate a declared finite set of simple waveform statistics on labeled agent-audio, silence, and forced-stop calibration segments. Select the simplest threshold that detects every labeled forced stop and produces no premature stop on the calibration agent fixture. Report all candidates and failures. | Freeze before mode-labeled qualification; no adjustment after viewing comparative outcomes |
| Sustained-silence hold duration | Evaluate 100–1000 ms in declared fixed increments against natural-pause and forced-stop calibration captures. Select the shortest duration with no natural-pause false stop in the calibration set. | Freeze with the threshold; changing it invalidates and restarts qualification |
| Analysis window and channel count | Derive a finite candidate set from observed codec/sample metadata. Require a window short enough to locate stop while preserving the no-false-stop criterion. | Freeze exact samples/window and channel handling before qualification |
| Capture channel separation | Accept only a topology that exposes an isolated returned-agent channel plus a stimulus reference or otherwise proves unambiguous separation with synthetic calibration signals. | Failure to prove separation is a no-go, not permission to weaken the endpoint |
| Codec and framing | Measure actual negotiated/runtime format and validate decoding with known fixtures; record codec, sample rate, sample width, channels, and frame duration. | Freeze before qualification and reject configuration mismatches |
| Loopback and signal contamination | Run stimulus-only, agent-only, and no-stimulus calibration captures. Demonstrate that cross-feed cannot satisfy the frozen stop or speech-bearing criteria. | Failure is a feasibility failure; do not tune using final condition results |
| Natural-end reference | Use a deterministic agent-audio fixture and uninterrupted baseline captures to establish the scripted natural end. | Freeze the fixture and hash before qualification |

Qualification may validate that frozen detector settings behave as expected. If
they do not, qualification is discarded, calibration is repeated without
examining between-condition effects, and all affected qualification trials are
rerun after a dated review.

## 8. Internal milestone measurements

These are intended future records, not claims that the inspected code already
instruments them.

### `telnyx-onset` milestones

| Milestone | Required fields |
|---|---|
| Stimulus start at harness | Run/trial ID, fixture hash, harness monotonic timestamp |
| First inbound speech-bearing frame, when detectable | Agent-local monotonic timestamp, frame index, frozen criterion version |
| VAD decision | Agent-local monotonic timestamp, VAD configuration/version |
| First eligible interim transcript | Agent-local receipt timestamp, redacted/synthetic event metadata |
| First eligible final transcript | Agent-local receipt timestamp, event metadata |
| Interruption requested | Agent-local timestamp, active response generation, source |
| Interruption source | Exactly one of the condition-eligible sources; observed ineligible signals recorded separately |
| Local media epoch invalidated | Agent-local timestamp, old/new epoch, response generation |
| Clear send started | Agent-local timestamp, response generation |
| Clear send completed or failed | Agent-local timestamp, outcome/error category |
| Response cancellation requested | Agent-local timestamp, generation |
| Response teardown completed | Agent-local timestamp, generation |
| Caller turn completed | Agent-local timestamp, turn ID, source event count |
| Next response started | Agent-local timestamp, generation, caller-turn ID |
| Harness-side acoustic stop | Harness monotonic timestamp, detector version and parameters |

### `telnyx-offleash` equivalents

The verified architecture exposes transcript receipt and a Call Control
`playback_stop` request. Historical raw artifacts also contain harness-leg
`call.speak.started`, transcription, `barge_stop_issued`, and provider
`call.speak.ended` records. The current inspected checkout does not wire the
historical controller/metric sink into the running CLI/server, so new collection
must re-establish and qualify those records rather than assume it is ready.

| Milestone | Use |
|---|---|
| Stimulus start | Common harness timestamp when rerun; historical provider `call.speak.started` remains a legacy control-plane boundary |
| First transcript event | Agent receipt plus provider timestamp when available |
| Playback-stop requested | Agent-local monotonic timestamp before the request |
| Relevant provider events | Diagnostic only unless their clock domain is used solely with compatible provider events |
| Harness-side acoustic stop | Required for a new common-boundary headline comparison |

Only timestamps from the same monotonic clock may be subtracted directly.
Agent-local decompositions and harness-boundary durations are separate metric
families unless Phase 2 proves clock co-location and timestamp handoff semantics.

## 9. Clock model

- Duration calculations use monotonic clocks.
- UTC or wall-clock timestamps are provenance only.
- Timestamps from unsynchronized hosts are never subtracted.
- Provider timestamps, webhook receipt timestamps, agent-local timestamps, and
  harness timestamps are not treated as one clock.
- Provider events remain useful diagnostics when they are not the headline
  endpoint.
- Agent-local milestone durations are labeled separately from harness-boundary
  durations.
- A clock identifier and process/host identifier accompany every raw timestamp.
- Any bridge between agent and harness clocks must be explicitly proven; shared
  wall time is not sufficient.

The historical offleash headline used Telnyx `occurred_at` from harness-leg
`call.speak.started` to agent-leg `call.speak.ended`. Its component timings used
host-monotonic receipt/request values. That control-plane result cannot appear in
the new acoustic headline column unless `offleash-transcript` is rerun with the
approved common harness boundary.

## 10. Trial lifecycle and eligibility

### Lifecycle

1. **Call and media ready:** required sockets/control legs and capture paths pass
   readiness checks; configuration matches the declared condition.
2. **Agent audio confirmed active:** the harness detector confirms returned agent
   audio for the active scripted response generation.
3. **Stimulus scheduled:** the deterministic order and barge offset identify the
   planned emission.
4. **Stimulus emitted:** the harness records the first non-silent emitted sample.
5. **Trigger observed:** eligible and ineligible signals are recorded with source.
6. **Interruption action observed:** exactly one generation-scoped action is
   requested and its local/provider outcome is recorded.
7. **Harness-side acoustic stop observed:** the frozen detector locates a stop or
   reports no stop.
8. **Caller turn confirmed:** turn assembly is checked for exactly-once content.
9. **Trial classified:** the immutable raw record is marked eligible/ineligible,
   successful/unsuccessful, and assigned every applicable failure category.

### Populations

**Attempted trial:** every scheduled benchmark attempt after the run declares
the call and media path ready.

**Eligible trial:** an attempt in which agent audio was confirmed active at
stimulus start, the synthetic fixture was emitted as intended, and required
capture and event records exist.

**Successful interruption:** an eligible trial satisfying all of the following:

1. Agent audio was active when the stimulus began.
2. The selected condition-eligible trigger fired.
3. Exactly one interruption action was initiated for the active response
   generation.
4. Harness-side agent audio stopped before the scripted utterance's frozen
   natural end.
5. The caller stimulus became exactly one completed user turn.
6. Old response audio did not resume after the stop.
7. When required by the scenario, the next response was based on that interrupted
   caller turn.

Stopping playback while losing or duplicating the caller utterance is a failed
interruption. Latency distributions may be calculated over successful eligible
trials, but every report must also show attempted, eligible, and successful
counts; every failure category; and success rate. Failed attempts are never
silently deleted or replaced.

## 11. Failure taxonomy

Every attempted trial remains in raw records and may carry multiple failure
labels. Ineligibility is determined without reference to the observed latency.

| Failure code | Default classification | Meaning |
|---|---|---|
| `agent_never_spoke` | Ineligible | Required agent fixture never became active before the scheduling timeout |
| `stimulus_started_without_agent_audio` | Ineligible | Stimulus began without confirmed active agent audio |
| `stimulus_delivery_failed` | Ineligible | Fixture did not emit as intended |
| `media_capture_missing` | Ineligible | Required waveform/reference channel is absent or corrupt |
| `trigger_not_observed` | Eligible, unsuccessful | No condition-eligible trigger was observed |
| `interrupt_action_not_observed` | Eligible, unsuccessful | Eligible trigger occurred but no interruption action followed |
| `duplicate_interrupt_action` | Eligible, unsuccessful | More than one interruption action was initiated for the active response generation |
| `natural_utterance_end` | Eligible, unsuccessful | Agent audio ended naturally after a valid stimulus rather than from the selected action |
| `acoustic_stop_not_found` | Eligible, unsuccessful | Required capture exists but the frozen detector finds no sustained stop |
| `caller_turn_lost` | Eligible, unsuccessful | Synthetic caller content did not become a completed turn |
| `caller_turn_duplicated` | Eligible, unsuccessful | The stimulus produced more than one committed caller turn |
| `stale_audio_resumed` | Eligible, unsuccessful | Audio from the interrupted response returned after the detected stop |
| `false_interruption` | Eligible control failure | An interruption occurred without the scripted caller stimulus or selected eligible source |
| `call_transport_failure` | Ineligible if required records cannot exist; otherwise eligible failure | Call/media transport failed; classification records the lifecycle point |
| `invalid_event_order` | Ineligible | Required events cannot be ordered consistently under the declared clock model |
| `instrumentation_failure` | Ineligible | Required measurement records are missing or internally inconsistent |
| `configuration_mismatch` | Ineligible | Recorded code/configuration does not match the declared condition |

An agent utterance that naturally ends just after stimulus start remains an
eligible unsuccessful trial if all required records exist. It is not removed to
improve latency. Repeated timing collisions must be reported. They may motivate
only a dated amendment under Section 1: any already captured affected runs are
invalidated and restarted, not reclassified or pooled under the new method.

## 12. Half-duplex diagnostic scenarios

Half-duplex results are suppression and floor-acquisition outcomes. They are not
entered into a full-duplex barge-in latency distribution.

### Short-overlap scenario

The complete synthetic caller utterance begins and ends while the half-duplex
gate remains closed.

Expected policy behavior:

- No true interruption.
- Caller media is intentionally suppressed.
- No complete caller turn is expected from that attempt.
- Any trigger during the closed interval is unexpected and investigated as
  stale input, contamination, or an implementation defect.

Measurements:

- Gate-closed duration.
- Suppressed audio duration and frame/sample count when observable.
- Whether any caller turn appears.
- Whether the script requires a repeat attempt.
- Time until the caller can successfully acquire the floor in the scripted
  repeat scenario.

### Boundary-crossing scenario

A longer caller fixture begins while the gate is closed and continues past agent
playback plus the guard interval.

Measurements:

- Suppressed prefix duration.
- First accepted caller-audio boundary.
- Whether the remaining tail produces a turn.
- Whether the transcript is absent, clipped, or complete against the known
  synthetic text.
- Whether semantic intent survives and drives the expected next response.

## 13. No-stimulus and false-interruption controls

No-caller-speech trials run while agent playback is active. For the controlled
network harness, a VAD-triggered interruption without a caller stimulus is a
`false_interruption` or signal-contamination event. These controls are required
before a low VAD latency can be interpreted as useful responsiveness.

The clean controlled-harness experiment and physical-device echo diagnostics
are reported separately. Passing a network no-stimulus control does not prove
speakerphone, handset, television, or arbitrary endpoint safety and does not
establish acoustic echo cancellation.

## 14. Sample and stopping policy

Counts are attempted trials, not target successes. Failed trials are not
replaced.

| Stage | Condition/scenario | Attempted trials | Policy |
|---|---|---:|---|
| Calibration/qualification | Each primary full-duplex condition | Up to 10 each | Validate operation, record completeness, strict trigger eligibility, and the already frozen detector. Excluded from final comparison. No tuning toward a preferred effect. |
| Final matched comparison | `onset-fd-vad` | 40 | Interleaved or deterministically randomized with transcript condition |
| Final matched comparison | `onset-fd-transcript` | 40 | Same run design; ordering seed recorded |
| Secondary | `offleash-transcript` | 40 | Only if rerunnable through the approved common acoustic boundary; otherwise historical context only |
| Diagnostic | Half-duplex short overlap | 20 | Suppression/floor-acquisition outcomes |
| Diagnostic | Half-duplex boundary crossing | 20 | Prefix clipping/intent-survival outcomes |
| Control | No stimulus during active playback | 20 | False-interruption and contamination outcomes |

The default final design uses a recorded deterministic randomization seed and
interleaves the two primary conditions in balanced blocks. The exact block
construction is frozen before final collection. No run continues until a desired
number of successful trials. Any count change requires a dated amendment before
affected final comparative data are inspected and may not be motivated by an
effect appearing weak, strong, or noisy.

## 15. Experimental controls

Later phases must freeze and record the following for every formal run:

- Git commit and clean-tree status.
- Benchmark mode.
- Synthetic caller fixture, version, text, duration, and cryptographic hash.
- Deterministic agent utterance/response fixture and hash.
- Barge offset and scheduling rule.
- TTS voice and all relevant settings.
- STT engine, model, language, interim/final, and endpointing settings.
- VAD implementation, version, frame accumulation, and thresholds.
- Audio codec, sample rate, sample width, channel count, and frame duration.
- Telnyx region or equivalent call-route details when available.
- Half-duplex guard duration.
- Acoustic-stop detector statistic, threshold, window, hold duration, and version.
- Run-order seed and generated condition order.
- OS, Python/runtime, and dependency versions.
- A redacted non-secret configuration snapshot.

The matched onset conditions must differ only in trigger eligibility. A
verification record must show that VAD cannot interrupt in
`onset-fd-transcript` and that transcripts cannot initiate any interruption in
`onset-fd-vad`. Any other configuration difference is a
`configuration_mismatch` and disqualifies the trial from the primary matched
analysis. A prospectively declared additional difference defines a separate
comparison; disclosure does not make it part of the matched comparison.

## 16. Analysis and reporting policy

Pre-registered outputs are:

- Attempted, eligible, and successful counts.
- Success rate with numerator and denominator.
- Failure counts by category, including overlapping categories.
- Harness-boundary interruption latency median, interquartile range, and p90.
- A 95% percentile bootstrap confidence interval for the median using 10,000
  resamples and a recorded analysis seed.
- Median between-condition difference. It is unpaired for independent trials;
  a paired estimate is used only if Phase 2 prospectively establishes true
  matched trial pairs and records that design before final collection.
- Trigger-to-action agent-local decomposition.
- Action-to-harness-side-acoustic-stop decomposition only when clock compatibility
  is proven; otherwise the milestones are reported without subtraction.
- Caller-turn loss and duplication rates.
- Duplicate interruption-action count and rate by condition.
- False-interruption rate.
- Half-duplex suppression duration, clipped-prefix, floor-acquisition, repeat,
  and intent-survival outcomes.

p99 is not a centered or headline statistic with only a few dozen trials. Every
latency chart or table must display, or directly accompany, the relevant success
rate and failure counts. A faster median with materially worse correctness is
reported as a latency/correctness tradeoff, not simply a win.

Qualification data, final data, and historical offleash data remain separate.
The historical 1328 ms offleash control-plane result may be contextual text or a
separately labeled legacy table, never a value in the new acoustic headline
column.

## 17. Data integrity, privacy, and security

- Use synthetic benchmark speech only.
- Never commit API keys, bearer tokens, WebSocket credentials, phone numbers, or
  unredacted provider identifiers.
- Do not include ordinary caller transcripts or tool arguments in general
  benchmark telemetry.
- Do not commit live call audio by default.
- Local audio captures are bounded, protected by normal filesystem permissions,
  and must be gitignored before capture tooling is introduced.
- Raw event records may be committed only after deterministic redaction or when
  they contain exclusively synthetic, non-sensitive data.
- Replace call IDs with run-local opaque identifiers or salted hashes. Store no
  published salt that reverses identifiers.
- Benchmark mode comes from trusted configuration, never caller-controlled input.
- Any future media-injection endpoint is authenticated, short-lived, and scoped
  to the intended call.
- Raw trial data are append-only/immutable after capture. Corrections create a
  new version with provenance; derived analysis is generated separately.
- Every public artifact receives an explicit secret and personal-data review.

These are policies for later implementation. This phase adds no capture,
injection, telemetry, or endpoint code.

## 18. Threats to validity

| Threat | Control, reporting, or limitation |
|---|---|
| Provider and network variability | Interleave primary modes, record time and route metadata, report run/block distributions; residual variability remains |
| Region and route variability | Hold account/application/route constant where possible and record available region details; do not generalize beyond observed paths |
| STT service variability | Pin engine/settings and interleave conditions so both primary modes experience the same service period |
| TTS differences between architectures | Hold TTS fixed only in the primary comparison; report it as an architectural difference in the offleash comparison |
| Different stop primitives | Hold clear fixed in the primary comparison; treat `clear` vs `playback_stop` as part of the whole-system difference |
| Acoustic detector selection | Use bounded, condition-independent calibration and freeze before qualification/final trials |
| Natural pauses in agent speech | Use deterministic audio and a sustained-silence detector validated against uninterrupted baseline captures |
| Natural utterance end near stimulus | Freeze offset/fixture, retain collisions as failures, and report their rate |
| Echo and signal contamination | Require no-stimulus and isolated-channel calibration; report device echo diagnostics separately |
| Caller fixture representativeness | Use a published synthetic fixture; conclusions apply to that fixture and declared variants, not all speech |
| VAD threshold and utterance-duration sensitivity | Pin the primary VAD; any sensitivity analysis is labeled secondary and cannot replace the registered result |
| Network harness vs physical endpoints | Limit conclusions to the controlled harness; do not claim arbitrary speakerphone safety |
| Historical offleash endpoint difference | Keep historical control-plane results separate or rerun offleash through the common acoustic boundary |
| Small sample for tails | Report median/IQR/p90 and uncertainty; do not center p99 |
| Instrumentation overhead | Measure/log overhead where feasible, keep it identical across primary modes, and report limitations |

## 19. Non-goals

This benchmark does not attempt:

- General-purpose acoustic echo cancellation.
- Production certification across arbitrary endpoints.
- Classification of backchannels, coughs, television audio, nearby speakers, or
  other semantic/noise categories.
- A reusable conversation state-machine framework.
- Redesign of the media pacer.
- Treatment of provider noise suppression as guaranteed AEC.
- A human perception study.
- Universal vendor or platform claims.
- Retrospective metric, eligibility, detector, or sample-count changes after
  final results are viewed.

## 20. Phase gates

### Gate to begin Phase 2

Phase 2 may begin only after review confirms:

- Research questions and condition definitions are unambiguous.
- The matched, whole-system, and policy-diagnostic comparison hierarchy is
  accepted.
- Trial eligibility, success, and failure rules are explicit.
- The clock model prohibits incompatible subtraction.
- The common acoustic boundary is explicitly unproven.
- Each detector/capture unknown has a bounded calibration and freeze rule.
- Half duplex is treated only as a policy diagnostic for overlap behavior.
- Privacy, raw-data integrity, and endpoint-security constraints are declared.

### Gate to benchmark-ready runtime and final collection

Runtime implementation may not be described as benchmark-ready, and final data
collection may not start, until later phases prove:

- One common harness can emit the stimulus, capture returned agent audio, and
  timestamp both on one monotonic clock.
- The frozen detector finds a sustained harness-side acoustic stop without
  misclassifying natural pauses in qualification captures.
- Interrupted caller turns are preserved exactly once and drive the required
  next response.
- Cancellation, speaking state, interruption state, and teardown are owned by
  the active response generation.
- Strict matched trigger eligibility exists and all other primary-condition
  settings match.
- Trial collection, raw records, classification, and analysis are repeatable.

Failure of the common-boundary gate is not permission to substitute a provider
event silently. It requires a dated amendment, another methodology review, and a
new decision about whether runtime benchmark work should continue.

## 21. Amendments

### Amendment 1 — 2026-07-12: external SIP media-endpoint harness

**Status:** revision 2, drafted; NOT in force until it passes a final
methodology review. A first independent methodology review (five adversarial
lenses: plan consistency, measurement validity, evidence audit,
security/operations, implementability) was completed 2026-07-12 and returned
twenty-eight findings; all are incorporated in this revision and in
`bench/SIP_HARNESS_SPEC.md` revision 2. No qualification or final comparative
data have been collected under any capture path, so no captured data are
invalidated; the Phase 2 live-attempt record is retained as evidence and is
never pooled with measurement data.

**What failed.** The Phase 2 hard gate — one common harness that emits the
stimulus, captures returned agent audio, and timestamps both on one monotonic
clock — could not be satisfied by the original capture path, which observed
provider media streams on Telnyx Call Control legs. Twenty-six bounded live
attempts established the controlling provider behaviors on this account and
topology (evidence in `bench/PHASE2_REPORT.md`):

1. A call leg carrying a bidirectional media stream exposes no usable
   provider-outbound track on any stream attached to that leg: attempts 10
   and 11 (the probe leg's own bidirectional stream), attempts 8 and 22 (the
   agent leg's own bidirectional stream), and attempt 26 (a sibling
   receive-only stream on the same leg, with caller-line delivery
   demonstrably flowing). Attempt 24 also observed zero outbound frames but
   is confounded with behavior 4 below and is cited only there.
2. Inbound-direction surfaces on the agent leg mirror the agent's own
   websocket-injected audio: attempt 22 measured the mirror on the
   bidirectional socket's inbound track within approximately one millisecond
   of delivery to the opposite leg, and attempt 24 extended it to the
   separate receive-only monitor stream's inbound track (identical active
   window and peak). Attempt 21's `self`-target recirculation loop was a
   distinct, since-explained phenomenon and is not cited for the mirror.
3. Receive-only streams inherit the leg's media context and do not honor
   codec overrides: attempts 13, 14, and 23. Attempt 25 showed the adjacent
   behavior that an attached stream's start metadata reports the leg's media
   context even on a bidirectional stream.
4. A Call Control-answered harness endpoint transmits no RTP, so
   delivery-gated provider tracks starve: attempt 24.

The production agent requires its bidirectional stream, so behaviors 1 and 2
jointly preclude any clean stimulus-reference channel on the agent leg, and
behavior 1 precludes feeding the agent leg caller audio without destroying
the returned-agent channel on the harness leg (attempt 26). Per Section 20,
this failure is not permission to substitute a provider event; it requires
this dated amendment and another methodology review.

**What changes.**

1. *Capture topology.* The measurement harness becomes an external SIP media
   endpoint: a local SIP user agent on a dedicated Telnyx SIP credentials
   connection places the call to the agent number, terminates media itself
   with the negotiated codec pinned to PCMU, continuously transmits the
   caller line, and records both directions locally. Emission and capture
   share one process and one monotonic clock by construction. The Call
   Control application, webhook workflow, and provider media streaming are
   removed from the measurement path; the agent side keeps the untouched
   production runtime. Full specification: `bench/SIP_HARNESS_SPEC.md`.
2. *Track logic.* The joint two-track selection rule ("one and only one
   track must carry the greeting; one and only one other track must match
   the known fixture") is superseded under Section 7's pre-registered
   allowance for "an isolated returned-agent channel plus a stimulus
   reference." The returned-agent channel is the received media recorded at
   the harness; the stimulus reference is the transmitted media recorded at
   the harness, which corresponds to the fixture by construction rather than
   by correlation. Consequently the returned-fixture correlation gate is
   replaced by an explicit transmit-delivery confirmation gate
   (`stimulus_delivery_failed` on unaccounted transmit counters), and the
   categories `track_ambiguous`, `fixture_match_missing`,
   `fixture_match_ambiguous`, and `cross_channel_alignment_failed` are
   retired as structurally unreachable. The specification enumerates the
   full taxonomy disposition, including new named categories for receive
   timeline discontinuities and post-stimulus echo.
3. *Greeting anchor.* The bench-only causal `greeting_output_started` gate
   (added after live attempt 6 to exclude pre-greeting setup media) is not
   observable at a SIP harness. Window A is instead anchored on received
   activity that satisfies a sustained-activity rule, with early media
   excluded and the attempt-6 false-anchor failure mode explicitly guarded
   as specified. This is a disclosed change to the anchoring mechanism, not
   to any detector threshold or window definition.
4. *Metric composition.* Harness-boundary interruption latency measured at a
   SIP endpoint physically includes both one-way media transits (stimulus
   toward the agent; returned audio toward the harness) and the harness's
   bounded receive-path delay. The metric definition is unchanged — both
   endpoints remain harness-boundary events — but durations measured under
   this amendment must never be pooled with, or directly compared against,
   provider-boundary or prior-topology durations. A per-trial transit
   covariate is recorded alongside, and never subtracted from, the headline
   metric.
5. *Emitted-stimulus artifact.* The transmitted stimulus is an 8 kHz,
   PCMU-encoded derivative of the canonical 16 kHz fixture, produced by a
   frozen anti-aliased decimation rule. The derived waveform is hashed and
   recorded as a first-class fixture artifact, and the emission boundary is
   defined against the first active sample recomputed on the derived
   waveform. The canonical 16 kHz fixture and hash are retained for
   provenance.

**Why this strengthens rather than weakens the registered method.** The
primary endpoint is defined at "the controlled harness boundary." The prior
path approximated stimulus emission with a provider playback command fetched
over HTTPS — a provider-side handoff — and approximated capture through
provider stream forks. Under this amendment both endpoint events are genuine
harness-boundary events. Channel separation is satisfiable under Section 7's
stimulus-reference allowance, contingent on the Section 7 loopback and
cross-feed calibration captures being run under the SIP topology (including
a stimulus-echo control, which the Section 13 no-stimulus controls cannot
provide) before any detector freeze.

**What does not change.** The metric name and definition; the frozen detector
candidate structure and its bounded calibration rules; the Window A–D
definitions, thresholds, and fail-closed discipline; fixture identity,
hashing, and integrity rules for the canonical fixture; trial lifecycle,
eligibility, success criteria, and sample/stopping policy; the comparison
hierarchy and allowable claims; privacy, raw-data integrity, and
secret-handling rules; all Phase 2 promotion gates (manual waveform
agreement, bounded calibration, independent review) and the prohibition on
creating `bench/measurement_profile.json` before a manually confirmed,
independently reviewed live GO.

**Review requirements before any live use.** (1) Final methodology review of
this revision and the specification; (2) offline validation of the harness
control loop and analysis reuse to the existing suite's standards, including
the specification's required adversarial cases; (3) the Section 7
calibration captures under the SIP topology (no-stimulus, stimulus-echo,
agent-only) before any detector freeze; (4) a separately authorized bounded
live attempt under the same teardown-and-evidence discipline as attempts
1–26.

### Amendment 1 addendum — 2026-07-13: bounded, recorded loss voids

**Status:** revision 2, incorporating all eighteen findings of its own
three-lens methodology review (measurement validity, plan consistency,
implementation audit) completed 2026-07-13. As with Amendment 1: no
qualification or final comparative data exist under any capture path, so no
captured data are invalidated; live SIP attempt artifacts are retained as
evidence and are never pooled with measurement data.

**Process disclosure.** Live SIP attempts 1–4 were placed under the user's
explicit per-attempt authorization before Amendment 1's final review
sign-off. Attempts 1–3 exposed and fixed harness implementation defects;
attempt 4 is this addendum's motivating evidence. Their per-attempt record
is in `bench/PHASE2_REPORT.md` ("SIP harness live attempts"). This sequencing
is disclosed as a deviation from Amendment 1's review-before-live-use
requirement, resolved by this dated record.

**Superseded rule, quoted.** The first methodology review installed, and
this addendum overrides, the following spec §3 rule: "Loss, concealment,
duplicate, or jitter-buffer resize events are counted; any such event inside
Window C, the separating-silence interval, the sustained-silence stop
region, or Window D fails closed as `rx_timeline_discontinuity`." Motivating
evidence: live SIP attempt 4 passed every stage through separating silence —
the first attempt in the project to do so — then failed closed when exactly
one RTP packet of 521 was lost during the response window. Every other event
class from the superseded rule keeps a named fail-closed disposition below.

**What changes.** Bounded packet loss no longer fails a run outright:

1. Loss is detected from the stack's locally computed per-packet receive
   loss counter (not the ~5 s peer RTCP receiver reports), polled every
   100 ms, and localized to its poll interval. The interval end is padded by
   the pinned jitter-buffer depth plus one frame, because the counter moves
   at packet arrival while the concealed audio surfaces about one buffer
   depth later. The jitter buffer is configured fixed-depth and recorded.
2. A lossy interval is **voided**: its windows certify neither activity nor
   silence, every certification run resets across it, and every certified
   interval — the natural-stop hold, the separating silence, and the Window
   D echo-judged span — is checked for void overlap as a wall-clock
   interval, not merely at frame stamps. A poisoned hold restarts after the
   void; a poisoned echo span re-anchors Window D past it.
3. Analysis consumes a window only strictly before the scan watermark,
   which advances only when a poll interval's loss verdict is in (lagged by
   the void pad). The watermark is armed before the INVITE, so no live
   window is ever consumed verdict-free. A watermark frozen longer than 2 s
   fails closed as `rx_timeline_discontinuity` before any deadline category
   can misattribute a harness stall to agent behavior.
4. Stimulus overlap is judged inside the verdicted scan, so concealed audio
   can never indict the agent for talking over the stimulus; it voids
   instead.
5. Voids are recorded per event with host-time and rx-sample bounds in the
   manifest, and every frame-metadata row carries its rx sample offset, so
   the manual waveform reviewer can overlay exactly which recorded audio is
   stack concealment.

**Event-class dispositions** (complete coverage of the superseded rule):
bounded stream-stat loss → voided interval; loss beyond bounds → fail
closed; RTP sequence/timestamp anomalies surfaced to the session → fail
closed after Window A anchors (at the port surface these identities are
synthesized, so the stream-stat path is the live loss signal); loss-counter
decrements → clamped monotonically; jitter-buffer resize → precluded by the
pinned fixed-depth configuration, and any observed adaptation is a
configuration defect (`media_format_mismatch` class), not tolerated media.

**Declared bounds, justified.** More than **5 loss events** or more than
**1 s of voided timeline** fails closed as `rx_timeline_discontinuity`.
Basis: (a) the measured prior — attempt 4 saw 1 loss in 521 packets
(≈0.2%) over ~10 s; at that rate a 60 s call expects ~6 losses, and 5
events bounds a comparably noisy but usable call while a materially lossier
transport should fail; (b) delay budget — each void costs at most one
100 ms localization unit plus its pad plus one restarted hold (≤500 ms), so
5 voids delay certification by at most ~3.5 s, comfortably inside the 15 s
greeting horizon and 60 s cap; (c) the 1 s total caps the unknown fraction
of a ~60 s certified timeline below ~2%. Void widths are recorded
per-event, so a harness-induced wide interval (a stalled poll) is visible
as such rather than charged silently to the transport.

**What does not change.** Detector thresholds, hold durations, window
definitions, the emission boundary, all other fail-closed gates, and the
prohibition on synthetic silence: voided audio is excluded from every
certification, never substituted, and the recorded WAV keeps the stack's
own concealment output, declared per-sample via the manifest's void
intervals.

**Why this is not retry-until-pass.** The alternative was re-dialing until
a call happened to traverse a lossless path, which the plan prohibits.
Bounded voids make the measurement valid on realistic transport while
keeping every boundary certification loss-free by construction.
