# Phase 2 acoustic-boundary probe report

## Verdict

**NO-GO**

The offline remediation is implemented and tested. The original three attempts
could not expose two tracks on one socket. A separately authorized fourth call
exposed and corrected a transient dynamic-end handling error. A fifth corrected
call captured both channels for the hard call duration but never completed
greeting-track selection. A sixth diagnostic run reached greeting selection and
fixture transmission, but proved that pre-greeting setup audio had been selected
before the actual VoiceAgent greeting completed. A seventh causal-gated run then
proved that neither inbound-only leg stream carries a valid returned greeting.
An eighth run proved that `both_tracks` does not return WebSocket-injected audio
on the agent socket's provider-outbound track under this topology. It did,
however, expose causal post-greeting activity on the independently streamed
probe leg. A ninth cross-leg/`both_tracks` run retained exact diagnostic WAVs
and showed both inbound legs were quiet after greeting. A tenth run requested
probe-leg `both_tracks` but received 1,067 inbound and zero outbound frames.
The subsequent sanitized configuration audit found that the harness number is
assigned to a separate Call Control application, while the bench had originated
through the agent application's connection. Correct separate-application
origination was then tested and produced the same zero-outbound result.
Bidirectional WebSocket capture is exhausted; receive-only probe streaming plus
harness-served fixture playback remains under review.
No fixture match, manual waveform review, detector calibration, or empirical GO
was produced. Phase 3 remains blocked.

## Repository and scope

- Remediation base: `b861afdfe22201474a018a778803d9451d5c6c14`
- Live-probe revision after attempt-1 correction:
  `24e0b74` (`Defer probe stream until bridge readiness`)
- Dual-socket live revision: `e72c839` (`Align Phase 2 cross-leg media capture`)
- Agent-`both_tracks` live revision: `4b3f6fb`
  (`Capture both agent-leg media tracks`)
- Cross-leg/`both_tracks` live revision: `9d60c96`
  (`Measure Phase 2 across isolated call legs`)
- Probe-outbound live revision: `e17ec2f`
  (`Capture probe-leg outbound media`)
- Branch: `duplex`
- Methodology authority: `BENCHMARK_PLAN.md`, unchanged
- Production runtime modules modified: none
- `bench/measurement_profile.json`: absent by design
- Comparative benchmark results: none

The probe remains a separate bench-only FastAPI application. Production
`onset.server` dispatch and normal VoiceAgent behavior are unchanged. The bench
agent route constructs the normal VoiceAgent only for leg B; the probe route on
leg A performs capture and injection only.

## Repaired measurement workflow

The socket handler may finish only as
`CAPTURE_COMPLETE_PENDING_REVIEW`, `CONDITIONAL GO`, or `NO-GO`. It never writes
`GO` and never freezes a measurement profile. Promotion requires all of:

1. a separately authorized bounded live attempt;
2. manual agreement between waveform and derived energy evidence;
3. completion of the finite, condition-independent detector calibration;
4. an independent review of the code and sanitized live evidence.

Send completion is retained only as pacing diagnostics. A returned mark is
retained only as supporting provider queue evidence. Neither defines an
acoustic endpoint.

## Window and track rules

Track names are not interpreted as direction. Both stable labels must cover a
common interval before selection. The probe evaluates them jointly:

- Window A: agent-only greeting;
- Window B: post-greeting silence/noise floor;
- Window C: verified returned fixture plus separating silence;
- Window D: genuinely new post-stimulus agent response.

The greeting track is not selected when it completes a detector window first.
Both tracks are truncated to their common completed diagnostic interval for the
joint decision. One and only one track must carry the greeting and later
response. One and only one other track must match the known fixture.

Fixture correspondence uses an explicit, deterministic 20 ms energy-envelope
correlation over unchanged mono PCM16/16 kHz samples. Alignment is searched only
over the finite 0–2000 ms range; L16 media is not resampled, reordered,
gap-filled, or silently reconstructed. The receive-only PCMU topology applies
the separately reviewed deterministic normalization described below. The
post-stimulus boundary opens only after the
matched returned fixture ends and 100 ms of separating silence is observed on
both tracks. Window D must contain new activity on the selected agent track and
no mirrored active response on the selected stimulus track.
Delayed provider-queued fixture audio therefore moves the boundary rather than
becoming a response.

Named NO-GO outcomes include `fixture_match_missing`,
`fixture_match_ambiguous`, `stimulus_overlap`,
`stimulus_boundary_ambiguous`, `track_ambiguous`,
`cross_channel_alignment_failed`, and `post_stimulus_response_not_observed`.

For every track and A–D window, derived evidence reports active-frame count,
frame count, RMS dBFS, noise-floor estimate, peak absolute level, clipping
count, and global/track-local gap, regression, and duplicate counts.

## Readiness and authentication

The controller owns bridge-ready and dual-media-ready events. Each socket may
authenticate and validate its own start and media format, but Window A analysis
and normal VoiceAgent startup require both validated starts, and stimulus
emission also waits for `call.bridged`. The waits use the existing state timeout.
Failure is named and followed by bounded teardown of both identified legs.

Only one probe socket can be active. Header and connected-frame authentication
are preserved. When the header is absent, connected-frame receipt and token
consumption are covered by a short explicit timeout. Capture state is allocated
only after successful authentication. Tokens remain random, one-use,
monotonic-expiring, run-bound, call-bound, route/role-bound, and constant-time
compared. A probe/leg-A token cannot authenticate the VoiceAgent route.

Offline FastAPI/WebSocket tests cover stalled and missing authentication,
expired/mismatched/reused tokens, one active socket, call-ID and format
mismatch, bridge readiness, socket errors, and both-leg teardown. No provider
is contacted.

## Fixture and capture integrity

The fixture loader rejects symlinks and non-regular files. It checks the stat
size before opening, performs one bounded read of at most the limit plus one
byte, and compares file identity and size before and after the read. Growth or
replacement fails closed. WAV/PCM16/mono/16 kHz/duration/hash/all-silence checks
remain. The original path is never persisted. An offline oversized-file test
proves rejection occurs before payload reading.

Global sequence continuity is updated for every lifecycle frame carrying a
sequence number, including start, media, mark, DTMF, error, and stop. Chunk and
media timestamp state remain track-specific. Arrival order remains primary;
real gaps, regressions, duplicates, and timestamp anomalies remain NO-GO. Tests
cover legal media → mark/DTMF → media interleavings.

Raw JSONL remains append-only. A failed attempt rewrites only the derived
manifest with a named category, sanitized terminal outcome, attempt number, and
teardown result. Tokens, phone numbers, provider IDs, transcripts, raw error
bodies, and authorization values are excluded.

Sanitized frame metadata records per-frame RMS dBFS, peak absolute level, and
clipping count. For the final authorized synthetic diagnostic only, exact
bounded channel PCM may also be written as neutral-name WAVs after both
authenticated sockets pass call binding and their role-specific exact-format
validation. These
ignored local 0600 files are diagnostic evidence, never committed or sufficient
for promotion; persistence is atomic, no-follow, best-effort, and cannot block
call teardown. No transcripts are retained.

The live artifact root is anchored to the module repository at
`bench/artifacts`, must resolve under the repository's `bench` directory, and
must match the exact path verified by live preflight. Symlinks and path escapes
are rejected. Run IDs remain generated; directories/files remain 0700/0600.

## Bounded detector calibration support

The offline helper evaluates declared finite statistic, analysis-window, and
activity/silence threshold sets, plus every sustained-silence hold from
100–1000 ms at a declared fixed step. The currently supported statistic is RMS
dBFS; unsupported declared statistics, invalid windows, and invalid threshold
orders are retained as named candidate failures rather than silently skipped.
It evaluates labeled natural-pause and forced-stop segments and records every
candidate, pass, and named failure. It does not select or persist a profile.
Calibration inputs and derived evidence remain local ignored artifacts, and an
independent review is required before any later profile freeze.

## Offline validation status

The remediation test suite includes adversarial delayed-fixture, unequal-prefix,
delayed/missing-channel, excessive cross-handler skew, simultaneous-route, and
one-frame response-skew cases. Similar waveforms on both tracks are ambiguous,
not first-completer wins. The full exact command results are recorded in the
task handoff that accompanies this report revision.

Minimal type-only corrections were made in `tests/test_media.py` and
`tests/test_tts.py`: collection variance was expressed with iterable/mapping
interfaces, and an async generator was narrowed for its existing `aclose`
assertion. Runtime code, assertions, and mypy configuration were not changed.

## Independent remediation review

An independent offline code review was completed on 2026-07-10 and persisted
here. Its initial pass found route-interchangeable tokens, one-track boundary
silence, incomplete stimulus-task failure retrieval, incomplete calibration
candidate accounting, and a one-frame Window D completion skew. Each issue was
corrected and covered by adversarial tests. The final re-review verdict was
**COMPLETE for the requested offline remediation**, with no remaining
correctness, security, privacy, or async-lifecycle blocker found.

This review is not the later independent review of live waveform, calibration,
and track-separation evidence. It does not promote Phase 2 to empirical GO.

An independent sanitized-evidence review was completed after the third live
attempt. It confirmed **NO-GO**: the single-track captures cannot establish the
required fixture-reference mapping or acoustic boundary, the attempt cap is
exhausted, `measurement_profile.json` must remain absent, and Phase 3 remains
blocked. The reviewer inspected no audio, provider identifiers, or secrets.

An independent sanitized-evidence review of the fourth call also confirmed
empirical **NO-GO**, while returning **GO** for the offline dynamic-end fix. It
independently reproduced 189/278 channel frames, the 92.318667 ms transient,
and fixed-helper replay with 187 aligned evaluations, two waits, and zero
errors. No audio, provider identifier, or secret was required.

An independent review of the fifth call confirmed empirical **NO-GO** and
returned **GO** for the bounded greeting-horizon and derived-energy evidence
fix. It verified the exact readiness-relative deadline, unchanged 20 ms
backdated detector result, at-most-180 ms control delay, full-cap stress bound,
and exclusion of PCM/content from the new metadata fields.

An independent review of the sixth call confirmed empirical **NO-GO** and
returned **GO** for the bench-only causal greeting-output gate. It verified that
candidate analysis, the explicit horizon, Window A, and final summary all begin
at the first VoiceAgent audio frame while production modules remain unchanged.

An independent review of the seventh call confirmed empirical **NO-GO** and
returned **GO** for the offline agent-leg `both_tracks` topology. The eighth
call then rejected its agent-outbound hypothesis. A follow-up independent
review confirmed that combining agent `both_tracks/self` routing with the two
isolated inbound leg captures was a distinct, plan-compatible topology. The
ninth call rejected that hypothesis. Its diagnostic WAVs leave one final
provider-supported direction to test: probe `both_tracks`, measuring only its
outbound track against agent inbound. Agent inbound alone still feeds the
VoiceAgent, and acoustic roles must be proven jointly and fail closed.

## Manual review procedure

After a separately authorized live run, inspect only its ignored local run
directory:

1. compare both waveforms across Windows A–D;
2. compare visible fixture end and response onset with energy and detector
   summaries;
3. confirm the fixture correlation and bounded alignment are credible;
4. confirm separating silence and absence of fixture overlap on the agent track;
5. confirm new agent activity begins only after the boundary;
6. record sanitized agreement or disagreement and obtain independent review.

This is waveform agreement validation, not a human-perception measurement.

## Live findings and remaining unknowns

- The original `target_legs=opposite` run produced 2,859 frames, all labeled
  `inbound` on its single probe socket.
- `target_legs=self` produced 2,834 frames, all labeled `inbound`.
- The separately authorized dual-socket `target_legs=opposite` run captured 189
  channel-A frames and 278 channel-B frames in expected L16/16 kHz format before
  its fail-closed stop. Its single transient dynamic-end comparison selected
  endpoints 92.3 ms apart; offline replay after changing that open window to
  wait-for-coverage produced 187 aligned evaluations, two transient waits, and
  zero alignment errors.
- The corrected retry cleared that transient gate and captured 5,228 frames:
  2,153 on channel A and 3,075 on channel B, with contiguous chunk counters and
  320-sample provider timestamp steps. Channel A processed about 43.06 seconds
  of PCM over 60.59 seconds of host time while channel B processed 61.50 seconds
  over 60.96 seconds. Code audit found that provisional full-history greeting
  analysis ran on every channel-A frame, creating quadratic handler work
  consistent with the observed backlog. The offline correction evaluates that
  provisional window every ten frames (200 ms) and fails closed after a fixed
  15-second greeting horizon anchored exactly to dual-media readiness. This
  preserves the detector's 20 ms backdated sample boundary, adds at most 180 ms
  of recognition/control delay, and bounds worst-case analysis work without
  changing any frozen boundary.
- The next diagnostic run selected channel B from a short setup burst, confirmed
  a false natural stop, and began fixture transmission while the real greeting
  was still being generated/injected. It captured 199 channel-A and 255
  channel-B frames. After stimulus start channel A had zero active frames while
  channel B had 167, then matching failed. The bench-only media adapter now
  records the first actual VoiceAgent output frame and anchors greeting analysis,
  Window A, and the full 15-second horizon to that causal event; pre-greeting
  media is excluded.
- The causal-gated run found channel A quiet in all 752 post-greeting frames
  (maximum -68.75 dBFS). Channel B had 15 active and 748 quiet frames, but its
  longest active run was two frames—below the five-frame/100 ms arm. It failed
  `agent_audio_not_observed`. This establishes that inbound-only streams cannot
  supply the returned VoiceAgent track under this topology.
- The agent-`both_tracks` run delivered zero provider-outbound frames. After
  causal greeting start, the probe socket was active in all 760 of 760 frames,
  beginning 9.678 ms later; agent provider-inbound was active in 384 and quiet
  in 384 of 768 frames. The persistent probe activity is a hypothesis, not role
  proof: the combined diagnostic must reject it if no natural acoustic stop or
  independent channel separation exists.
- Stable track separation and track-to-leg orientation remain unproven because
  the fourth and fifth calls ended before fixture transmission and the sixth
  selected setup audio rather than the real greeting.
- Framing jitter and ordering across a complete capture.
- Returned-fixture correlation under the live topology.
- Cross-feed and overlap behavior.
- Natural-stop and forced-stop detector agreement with waveform inspection.
- Whether the fixture produces a distinguishable later agent response.
- Which finite detector candidate, if any, passes all calibration labels.

## Post-NO-GO topology correction prepared offline

The inbound-only dual-socket, agent-socket outbound, combined
agent-`both_tracks`/cross-leg, same-app probe-outbound, and separate-app
probe-outbound hypotheses are empirically rejected. The configuration audit
found that both the outbound
harness leg and inbound agent leg had been originated/routed through the agent
application even though the harness number belongs to a distinct application.
Separate-application origination remains the required configuration baseline.
The remaining provider surface under review makes the probe stream receive-only
with `both_tracks` so bidirectional injection cannot suppress its outbound
track. It would deliver the fixture through a one-time authenticated HTTPS WAV
response from the same harness process, timestamping the first non-silent
response chunk on the same monotonic clock. It measures:

- neutral channel A: provider `outbound` media from the probe-leg socket;
- neutral channel B: provider `inbound` media from the agent-leg socket.

The receive-only provider surface does not honor L16 transcoding: the observed
start format was G.722/8 kHz/mono while the agent remained L16/16 kHz/mono. The
corrected request uses Telnyx's supported PCMU transcode target and requires an
exact PCMU/8 kHz/mono start. A local G.711 decoder maps each mu-law byte to its
standard signed PCM value and duplicates each 8 kHz sample once to form a 16 kHz
analysis timeline. This zero-order hold preserves 20 ms frame duration and
introduces no interpolated energy; the probe's acoustic boundary resolution
remains the original 8 kHz sample period. Known G.711 extrema/silence vectors,
frame size, route behavior, and metadata are tested. Any other start format
still fails closed.

The provider labels are used only to ensure that agent provider-inbound audio
alone is fed to the unchanged VoiceAgent. Probe provider-inbound and agent
provider-outbound events remain in socket-integrity diagnostics but are
excluded from acoustic measurement.
Agent/stimulus roles are still
proven jointly by greeting activity, silence, fixture correlation, and
post-stimulus response; no role is inferred from `inbound` or `outbound`. The
two sockets retain independent ordering domains, with track-local measurement
continuity. The first receive-only diagnostic deliberately stops with named
`stimulus_transport_pending` immediately after proving a returned greeting and
natural stop; it sends no fixture. The one-time HTTPS fixture transport is
implemented only if that prerequisite direction is empirically present, so an
absent outbound track cannot broaden the live surface unnecessarily.

The harness connection is required, nonempty, and distinct from the agent
connection. Immediately before dialing, read-only Telnyx lookups require both
numbers to be active and assigned to their expected connections. The dial
payload uses the harness connection, harness caller ID, agent destination, and
a per-call HTTPS webhook override derived from the authenticated WSS base. This
avoids mutating the harness application's account-level webhook; only the
already established temporary agent-application webhook workflow remains.
Identifiers and URLs are not written to artifacts or ordinary logs.

Every Window A-D boundary remains defined once in process monotonic time and
mapped separately to the first frame at or after that boundary on each neutral
channel. Unequal prefixes are discarded, missing common coverage fails closed,
and mapped boundary timestamps more than 40 ms apart fail as
`cross_channel_alignment_failed`.

Host receive time is sampled when each async WebSocket handler processes a
message, not at kernel/network arrival, so it is not sample-accurate provider
time. The 40 ms bound explicitly limits that scheduling uncertainty; a run
outside it is invalid rather than silently comparing different acoustic
intervals. A two-frame post-confirmation guard also prevents a late mirrored
frame from being omitted from the joint response check. The fixture endpoint is
exclusive: the matched channel starts silence at the exact end byte, while the
other channel conservatively advances past its corresponding boundary frame.
The 100 ms host interval must also contain at least 100 ms of PCM on each
channel; sparse delivery cannot satisfy the acoustic-duration requirement.

The harness waits for the bridge plus validated starts from both authenticated
media sockets before starting the normal VoiceAgent greeting. This is an
implementation correction within the plan's
pre-registered allowance for an isolated returned-agent channel plus a stimulus
reference, not a change to the metric, detector gate, eligibility rules, or
comparative conditions.

The original dual-socket correction was independently cleared on 2026-07-11.
On 2026-07-12, a follow-up independent review cleared the combined
cross-leg/`both_tracks` diagnostic subject to the same exclusive fixture
endpoint, absolute and cross-channel timing bounds, minimum acoustic silence,
independent socket ordering, teardown, privacy, and production-isolation gates.
The user has explicitly authorized iterative bounded test/fix calls. The
existing empirical verdict remains **NO-GO** until new evidence clears every
gate.

## Live attempt log

- 2026-07-11, attempt 1: **NO-GO — `stream_start_failed`**. The call reached
  both identified legs, then Telnyx rejected the leg-A probe stream before any
  media capture or track mapping. Both legs were hung up. The controller had
  requested `target_legs=opposite` before receiving `call.bridged`; the repaired
  workflow now starts that probe stream only after the bridge exists. No audio,
  comparative result, or measurement profile was produced by this attempt.
- 2026-07-11, attempt 2 (`target_legs=opposite`): **NO-GO — `call_hangup`**.
  Both media sockets and the expected L16/16 kHz format were reached, but all
  2,859 captured frame-metadata rows carried only the `inbound` track label.
  The hard call cap ended the attempt and both legs were hung up.
- 2026-07-11, attempt 3 (`target_legs=self`): **NO-GO — `call_hangup`**.
  The alternative target setting again reached media, but all 2,834 frame rows
  carried only the `inbound` track label. The hard cap ended the attempt and
  both legs were hung up.
- 2026-07-11, separately authorized attempt 4 (`target_legs=opposite`, revision
  `e72c839`): **NO-GO — `cross_channel_alignment_failed`**. Both authenticated
  sockets reached the expected media format and captured 467 total frames. A
  transient channel-A receive gap made the current open-ended greeting window
  appear 92.3 ms apart and the handler failed immediately. The call ended
  before track mapping or fixture transmission. Sanitized offline replay shows
  this endpoint was temporary: waiting for common dynamic coverage converts the
  same evidence to two waits and 187 valid alignments with no relaxed fixed
  boundary. Teardown was attempted; the manifest records `hangup_failed` after
  a leg had already ended. Its manifest `attempt_number: 1` is the ordinal
  within that one-attempt CLI process; “attempt 4” is the cumulative project
  log ordinal used in this report.
- 2026-07-12, separately authorized attempt 5 (`target_legs=opposite`, revision
  `3fd6960`): **NO-GO — `call_hangup`**. The corrected dynamic endpoint waited
  successfully and both channels remained connected through the hard duration,
  producing 5,228 sanitized frame rows. Greeting selection did not complete,
  so the fixture was not transmitted and no mapping or waveform artifact was
  written. Both legs were hung up. The manifest `attempt_number: 1` again means
  the sole attempt in this CLI process; this report uses cumulative ordinal 5.
- 2026-07-12, authorized iterative attempt 6 (`target_legs=opposite`, revision
  `6e967b4`): **NO-GO — `fixture_match_missing`**. The run selected channel B,
  confirmed a natural stop, transmitted the complete fixture, and received its
  mark, but did so before the real greeting finished injection. Derived evidence
  showed channel A remained quiet and channel B became strongly active. No
  fixture correlation or waveform artifact was produced; both legs were hung
  up. The causal greeting-output gate was then added offline.
- 2026-07-12, authorized iterative attempt 7 (`target_legs=opposite`, revision
  `39aa51c`): **NO-GO — `agent_audio_not_observed`**. The causal gate excluded
  setup media and waited the full greeting horizon. Channel A remained entirely
  quiet; channel B never exceeded two consecutive active frames. This rejects
  the inbound-only measurement topology. The agent-leg `both_tracks` hypothesis
  was prepared offline; whether Telnyx forks WebSocket-injected output back onto
  that stream's outbound track remains explicitly unproven.
- 2026-07-12, authorized iterative attempt 8 (`target_legs=opposite`, revision
  `4b3f6fb`): **NO-GO — `agent_audio_not_observed`**. Telnyx accepted
  `both_tracks`; the agent socket delivered 1,124 provider-inbound frames and
  zero provider-outbound frames, while the probe socket delivered 1,070 frames.
  The full causal 15-second greeting horizon elapsed before failure and both
  legs were hung up. This rejects the agent-socket outbound hypothesis for this
  revision/account/topology, not Telnyx media streaming universally. The probe
  socket's immediate post-greeting activity motivates the final bounded
  cross-leg/`both_tracks` diagnostic; persistent or mirrored activity remains a
  fail-closed result.
- 2026-07-12, authorized iterative attempt 9 (`target_legs=opposite`, revision
  `9d60c96`): **NO-GO — `agent_audio_not_observed`**. Both exact diagnostic
  WAVs validated as PCM16/16 kHz/mono. After causal greeting start, all 739
  probe-inbound measurement frames were quiet (maximum peak 1,557) and all 754
  agent-inbound frames were quiet (maximum peak 1,576). The attempt-8 probe
  activity was not reproducible. No fixture was transmitted; both legs were
  hung up, the webhook was restored, and the tunnel was stopped. This rejects
  the combined inbound-leg hypothesis and motivates testing the still-unseen
  probe provider-outbound direction.
- 2026-07-12, authorized iterative attempt 10 (`target_legs=opposite`, revision
  `e17ec2f`): **NO-GO — `agent_audio_not_observed`**. The probe socket accepted
  `both_tracks` but delivered 1,067 provider-inbound frames and zero
  provider-outbound frames; the agent socket delivered 1,141 inbound frames.
  No channel-A WAV or fixture transmission was possible. The diagnostic retained
  only the bounded channel-B WAV, both legs were hung up, the agent webhook was
  restored, and the tunnel was stopped. A read-only post-run assignment audit
  then exposed the same-application origination mismatch described above.
- 2026-07-12, authorized iterative attempt 11 (`target_legs=opposite`, revision
  `c54426b`): **NO-GO — `agent_audio_not_observed`**. Both active number
  assignments were verified, the call originated through the distinct harness
  application with a per-call webhook override, and the agent leg arrived
  through the agent application. The probe still delivered 1,071 inbound and
  zero outbound frames; the agent delivered 1,113 inbound frames. No channel-A
  WAV or fixture transmission was possible. Both legs were hung up, exact agent
  webhook restoration was verified, and the tunnel was stopped. This rejects
  separate-application bidirectional probe streaming.
- 2026-07-12, authorized iterative attempt 12 (`target_legs=opposite`, revision
  `a0e2ada`): **NO-GO — `media_format_mismatch`**. The receive-only probe
  stream connected, but its start metadata failed the exact L16/16 kHz/mono
  gate before the agent started or any media was processed. No fixture was
  transmitted; both legs were hung up, exact webhook restoration was verified,
  and the tunnel was stopped. A sanitized format-observation event is added
  offline so the next bounded run can distinguish provider codec/rate/channel
  behavior without weakening the gate.
- 2026-07-12, authorized iterative attempt 13 (`target_legs=opposite`, revision
  `615a352`): **NO-GO — `media_format_mismatch`**. Sanitized authenticated start
  metadata proved the agent socket was L16/16 kHz/mono and the receive-only
  probe socket was G.722/8 kHz/mono despite requesting L16. The mismatch failed
  before agent startup, media processing, or fixture transmission. Both legs
  were hung up, exact webhook restoration was verified, and the tunnel was
  stopped. PCMU/8 kHz normalization was then implemented offline; this run is
  not reinterpreted under the new decoder.

The original maximum-three-attempt policy was exhausted; attempt 4 used a fresh
explicit authorization. Attempt 9 additionally produced bounded ignored local
diagnostic WAVs under the documented exception. No mapped promotion track WAVs,
comparative results, or measurement profile were produced.

## Gate checklist

- [x] Bench-only application; production behavior unchanged.
- [x] Joint common-interval track selection.
- [x] Bounded returned-fixture matching and acoustic boundary.
- [x] Separating silence and new-response requirement.
- [x] Bridge-ready gate.
- [x] Bounded pre-allocation authentication.
- [x] Global lifecycle ordering plus track-local media ordering.
- [x] Sanitized failed-attempt manifest outcome.
- [x] Repository-anchored artifact path.
- [x] Finite calibration evaluation support without auto-selection.
- [x] Offline route/component/adversarial tests.
- [x] Three separately authorized bounded live attempts retained.
- [x] One separately authorized post-cap dual-socket attempt retained.
- [x] One separately authorized corrected dual-socket retry retained.
- [x] One authorized iterative diagnostic fixture attempt retained.
- [x] One authorized causal-greeting inbound-topology attempt retained.
- [x] One authorized agent-`both_tracks` topology attempt retained.
- [x] One authorized combined cross-leg/`both_tracks` attempt retained.
- [x] One authorized same-app probe-outbound attempt retained.
- [x] One authorized separate-app probe-outbound attempt retained.
- [x] One authorized receive-only format attempt retained.
- [x] One authorized receive-only sanitized format observation retained.
- [ ] Manual waveform agreement.
- [ ] Completed bounded calibration.
- [ ] Stable two-track live capture and separation.
- [x] Independent sanitized-evidence review confirms empirical NO-GO.

Phase 3 must not begin while any item remains unchecked. A live failure is
`NO-GO`, not permission to weaken the endpoint. `measurement_profile.json` may
be created only after a genuine, manually confirmed, independently reviewed
live GO.
