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
Bidirectional WebSocket capture is exhausted. A receive-only staged run has now
proved a returned greeting and natural stop on the joint channel candidate.
The narrowly scoped harness-served fixture playback required for the full
measurement call is implemented offline and awaits its bounded live attempt.
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
corrected request uses Telnyx's supported PCMU transcode target, forces PCMU in
the harness dial's preferred-codec list, and requires an exact PCMU/8 kHz/mono
start. A local G.711 decoder maps each mu-law byte to its
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
continuity. The first receive-only diagnostic deliberately stopped with named
`stimulus_transport_pending` immediately after proving a returned greeting and
natural stop; it sent no fixture. Because that prerequisite direction is now
empirically present, the one-time HTTPS transport is implemented. After natural
stop, the harness requests `playback_start` toward the opposite (agent) leg with
an explicit WAV type, one loop, provider caching disabled, and a high-entropy
run-bound URL. The GET is atomically consumed once, rejects Range and retries,
disables HTTP caching and live access logs, and never records the bearer URL. It
serves the exact bounded canonical WAV whose SHA-256 is in the manifest.
The first active PCM sample begins a dedicated ASGI response chunk; the same
process records monotonic time immediately before handing off that chunk and
records full-response completion or failure. Provider playback webhooks remain
diagnostic only and cannot define an acoustic boundary.

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

## Bidirectional target-leg determination prepared offline

Attempt 21 left one open provider question: whether the agent leg's
bidirectional target of `self` can recirculate injected media on a bridged
call. The Telnyx Call Commands API reference (Dial) answers it directly. It
documents `stream_bidirectional_target_legs` as "Specifies which call legs
should receive the bidirectional stream audio," with allowed values `both`,
`self`, and `opposite` and a default of `opposite`. Under that definition,
`self` on the bridged bench topology plays the agent's injected greeting back
to the agent leg itself — consistent with attempt 21's loud, replayed channel
B and exact-zero channel A — and never delivers it to the bridged harness leg.
`opposite`, the provider default, is the documented value that delivers
injected audio to the bridged peer, which is the leg the probe measures.

The production runtime keeps `self` unchanged: a single answered inbound call
has no opposite leg, which is why `onset/settings.py` overrides the provider
default there. That rationale does not apply to the bridged bench call. The
bench agent-leg stream now takes its bidirectional target from the explicit
`--target-legs` selection instead of hardcoding `self`; the value was already
recorded in the run manifest and now governs the agent leg. Exact-payload
tests were updated to pin the new agent-leg value. Under `opposite`, the
returned greeting and later response are expected on channel A (probe-leg
provider `outbound`) and the fixture on channel B (agent-leg provider
`inbound`); acoustic roles are still proven jointly from the waveforms, and
mirrored or recirculated activity remains a fail-closed result. No detector
threshold, silence requirement, time bound, or promotion gate was changed.

## Agent-leg receive-only monitor prepared offline

Attempt 22 proved that the agent leg's bidirectional socket surfaces the
websocket-injected greeting on its own provider-inbound track within about one
millisecond of its delivery to the opposite leg. While channel B is sourced
from that inbound track, the greeting window can never present exactly one
active track, so the injection mirror is a structural disqualifier for the
previous channel designation, not a transient provider fault.

The accumulated live evidence constrains the correction. Provider-outbound
tracks have delivered zero frames on every bidirectional socket that requested
them: the agent socket in attempts 8 and 22, and the probe socket in attempts
10 and 11. The only surface that has ever streamed a provider-outbound track
is a receive-only stream — the probe leg's PCMU stream has done so
continuously since attempt 15, including through silence, and in attempt 22 it
carried the delivered greeting. Telnyx's published limit restricts a call to
one bidirectional stream; it does not restrict an additional receive-only
stream.

The corrected topology therefore adds a third authenticated socket: a
receive-only `both_tracks` monitor stream on the agent leg, started with the
probe stream once the bridge exists. Measured channel B is now the monitor
stream's provider-outbound track — the audio Telnyx delivers toward the agent
leg. The design initially requested PCMU as on the probe leg, but attempt 23
proved the agent leg's receive-only stream delivers the leg's native
L16/16 kHz and does not honor a PCMU transcode request, so the monitor sends
no codec override and gates an exact L16/16 kHz/mono start with an exact
640-byte outbound framing gate and no normalization. Channel B therefore
carries native-rate PCM while channel A retains the reviewed G.711
normalization. The two measured channels remain directionally symmetric: each
is a leg's receive-only provider-outbound track, meaning the audio that leg's
party hears. Under `opposite`, the
greeting and later response are expected only on channel A, and the delivered
fixture only on channel B. The agent leg's bidirectional socket keeps feeding
provider-inbound audio to the unchanged VoiceAgent and retains its exact L16
gate, but both of its tracks are now diagnostic-only
(`agent_inbound_unmeasured`/`agent_outbound_unmeasured`) and cannot enter
promotion calculations. Monitor provider-inbound is likewise
diagnostic-only. Monitor tokens are route/role-bound like the existing
sockets, one monitor socket may be active, its ordering integrity joins the
completion gate, and the manifest now records each measured channel's
provider source. Acoustic roles are still proven jointly from waveform
evidence; mirrored or recirculated activity remains fail-closed. No detector
threshold, silence requirement, time bound, or promotion gate was changed.

Attempt 23 resolved the first open unknown: Telnyx accepts the second,
receive-only stream on a leg that already carries the bidirectional stream,
and its authenticated start arrived normally. Attempt 24 then resolved the
coverage unknown against this topology as-built: the monitor's outbound track
delivered zero frames because the Call Control-answered harness endpoint
transmits no RTP at all, so nothing is ever bridged toward the agent leg and
its delivery-gated outbound track has nothing to send. The same run proved
the injected-greeting mirror is a property of the agent leg's media rather
than of the bidirectional socket: the monitor's inbound track carried the
identical mirror.

The implemented correction makes the harness behave like a real caller with
an open line: a fourth authenticated socket (the keeper) starts a second,
bidirectional `inbound_track` stream on the probe leg and injects
continuously paced true-silence L16 frames with a hardcoded
`target_legs=opposite`, so the provider continuously delivers genuine
caller-side path audio toward the agent leg and the monitor's delivery-gated
outbound track has a stream to send. The keeper measures and persists
nothing; its tokens are route/role-bound like the other sockets, only one
keeper socket may be active, and the VoiceAgent greeting is gated on the
keeper actually transmitting so channel B has coverage from the first
greeting frame. The manifest records the keepalive parameters. Under this
shape every injection mirror stays on a diagnostic-only inbound track
(probe-leg inbound carries the keeper's entry mirror; agent-leg inbound
carries the agent injection's mirror), while both measured channels remain
each leg's delivered-audio surface. This is not inserted synthetic silence in
the prohibited sense: no captured waveform is modified, every measured sample
remains provider-delivered media, and an idle caller line transmitting
silence models the pre-registered scenario more faithfully than a dead
endpoint. The methodology-sensitive distinction is recorded here explicitly
for independent review. Attempt 26 resolved both open unknowns negatively:
with the caller line demonstrably transmitting, the monitor's outbound track
still delivered zero frames, and the probe leg's measured receive-only
outbound track collapsed to a single frame. Together with attempts 10, 11,
22, and 24, the provider rule on this account/topology is that a leg carrying
any bidirectional stream exposes no usable outbound track on any of its
streams, while every agent-leg inbound surface mirrors the websocket
injection. The production agent cannot give up its bidirectional stream, so
no measured channel B can exist on the agent leg, and holding the caller line
open destroys channel A on the probe leg. The Call Control media-streaming
surface is therefore exhausted for the plan's channel-separation requirement;
proceeding requires a Telnyx-side correction or the separately approved
benchmark redesign (an external SIP media-endpoint harness that terminates
media locally and captures both directions on one host clock). That redesign
is drafted as `BENCHMARK_PLAN.md` Amendment 1 with its specification in
`bench/SIP_HARNESS_SPEC.md`; both await methodology review.

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
- 2026-07-12, authorized iterative attempt 14 (`target_legs=opposite`, revision
  `a2452e0`): **NO-GO — `media_format_mismatch`**. Requesting PCMU on the
  receive-only stream alone still yielded G.722/8 kHz/mono because the call had
  negotiated G.722. The mismatch failed before agent startup or media. Both
  legs were hung up, exact webhook restoration was verified, and the tunnel was
  stopped. The harness dial now explicitly prefers PCMU before streaming starts.
- 2026-07-12, authorized iterative attempt 15 (`target_legs=opposite`, revision
  `eba46b5`): **NO-GO — `media_format_mismatch`**. PCMU negotiation succeeded:
  the probe exposed both tracks at PCMU/8 kHz/mono and the agent remained
  L16/16 kHz/mono. After 297 valid 160-byte probe-outbound frames, an unexpected
  source frame size triggered the exact framing gate. No fixture was sent. The
  channel-A WAV and metadata showed probe outbound quiet, while separate
  sanitized frame metadata showed probe inbound activity; provider labels
  therefore cannot select the acoustic role. Both
  legs were hung up, exact webhook restoration was verified, and the tunnel was
  stopped. A sanitized mismatch-length event is added before changing framing.
- 2026-07-12, authorized iterative attempt 16 (`target_legs=opposite`, revision
  `af6aea7`): **NO-GO — `cross_channel_alignment_failed`**. All observed probe
  frames passed the exact PCMU framing gate, but the first probe frame capable
  of representing causal greeting start arrived about 155 ms after the first
  agent frame. The alignment helper incorrectly compared both first frames
  directly with the requested boundary instead of discarding unequal prefixes
  and advancing to their latest common observable start, contradicting the
  documented common-interval rule. No fixture was sent. Both legs were hung up,
  exact webhook restoration was verified, and the tunnel was stopped. The
  common-start calculation was corrected offline without changing the 40 ms
  cross-channel skew limit applied after prefix discard.
- 2026-07-12, authorized iterative attempt 17 (`target_legs=opposite`, revision
  `0f2730c`): **NO-GO — `media_format_mismatch`**. PCMU and common-start setup
  succeeded, but a 156-byte frame on diagnostic-only probe inbound triggered the
  framing gate. All 124 measured probe-outbound frames were the declared 160
  bytes; channel A remained quiet, agent inbound was active, and sanitized probe
  inbound metadata was active. No fixture was sent. Both legs were hung up,
  exact webhook restoration was verified, and the tunnel was stopped. The exact
  160-byte gate is now scoped to measured outbound; variable diagnostic inbound
  remains integrity/metadata-only and cannot enter acoustic analysis.
- 2026-07-12, authorized iterative attempt 18 (`target_legs=opposite`, revision
  `318e18c`): **EXPECTED STAGED NO-GO — `stimulus_transport_pending`**. Both
  exact media-format gates passed. Joint common-interval analysis selected
  channel B, observed greeting activity, and confirmed its natural stop; the
  staged controller then halted before fixture transmission exactly as designed.
  Both legs were hung up, exact agent-webhook restoration was verified, and the
  tunnel was stopped. This clears the receive-only transport prerequisite but
  is not a Phase 2 GO. The one-time HTTPS fixture transport was then implemented
  offline for the next bounded full-measurement call.
- 2026-07-12, authorized iterative attempt 19 (`target_legs=opposite`, revision
  `e9195b8`): **NO-GO — `agent_audio_not_observed`**. Both exact media formats
  and both probe tracks were present, but the joint candidate gate could not
  prove a natural greeting stop. Channel A was silent; channel B had 600 active
  20 ms windows and no qualifying stop, while diagnostic-only probe inbound had
  298 active frames. The controller therefore failed closed at the unchanged
  15-second greeting limit before arming or serving the fixture URL. Both legs
  were hung up, exact agent-webhook restoration was verified, and the tunnel
  was stopped. Comparison with attempt 18's valid channel-B stop identifies
  non-reproducible returned-media activity, not an HTTPS transport defect; no
  detector threshold or time bound was changed.
- 2026-07-12, authorized iterative attempt 20 (`target_legs=opposite`, revision
  `551f50c`): **NO-GO — `agent_audio_not_observed`**. Exact media setup again
  passed, but this time channel B contained only sub-threshold noise. The TTS
  profile reported 61,440 decoded-source bytes across 147 provider chunks while
  the greeting path injected only two PCM frames; no returned greeting could be
  selected and no fixture URL was armed or served. Both legs were hung up,
  exact webhook restoration was verified, and the tunnel was stopped. The
  production-supported whole-buffer TTS decoder is now selected only by the
  bench live configuration. It retains the same target PCM format and decoder
  intent while removing provider MP3 chunk boundaries from incremental decode;
  this does not claim waveform equivalence between the two operational paths.
  Production startup, thresholds, clocks, and acoustic gates remain unchanged.
- 2026-07-12, authorized iterative attempt 21 (`target_legs=opposite`, revision
  `929b7f7`): **NO-GO — `agent_audio_not_observed`**. Whole-buffer greeting
  decode was provenance-recorded and produced a stable 154-frame injection, but
  the returned agent channel never reached natural silence. Channel A remained
  exact zero; channel B became clipped and repeated an exact two-second PCM
  cycle through the greeting deadline. One-second blocks at seconds 9, 11, 13,
  and later were byte-identical, as were the alternating blocks at seconds 10,
  12, 14, and later, while provider chunk, sequence, and timestamp fields kept
  advancing monotonically. This rules out local capture replay and makes the
  Phase 2 natural-stop prerequisite impossible on the current provider topology.
  No fixture URL was armed or served. Both legs were hung up, exact webhook
  restoration was verified, and the tunnel was stopped. Further unchanged
  retries are halted to avoid outcome fishing; the next step requires a Telnyx
  media/topology correction or a separately reviewed benchmark redesign.

- 2026-07-12, authorized iterative attempt 22 (`target_legs=opposite`, revision
  `f51e1ed`): **NO-GO — `track_ambiguous`**. The agent-leg bidirectional target
  used the documented `opposite` value for the first time. Both exact media
  formats passed, the greeting injected once, and the attempt-21 replay loop
  did not recur: loud audio spanned a single interval matching the injected
  frame count and then stopped naturally on both measured channels. Channel A
  (probe-leg provider outbound) carried strong returned agent audio for the
  first time in any attempt, confirming that `opposite` delivers injected
  audio to the bridged harness leg. However, channel B (agent-leg provider
  inbound) carried a near-simultaneous copy of the same greeting — first and
  last active frames within about one millisecond of channel A's, with
  matching level distributions and no clipping — so joint selection could not
  attribute the greeting to exactly one track and correctly failed closed
  before any fixture arming. The sub-millisecond simultaneity rules out an
  acoustic echo path and indicates the copies diverge at a provider mixing
  point: websocket-injected audio appears at its agent-leg entry (provider
  inbound) at the same instant it is delivered to the opposite leg. Agent
  provider-outbound again delivered zero frames, consistent with nothing yet
  played toward the agent leg. Both legs were hung up, exact webhook
  restoration was verified, and the tunnel was stopped. This replaces the
  attempt-21 replay blocker with a structural finding: while the agent injects
  on the same leg that supplies channel B, the greeting window cannot present
  exactly one active track under the current channel designation. Candidate
  correction under review: designate agent-leg provider outbound (the
  provider's delivery surface toward the agent) as the stimulus-reference
  channel and demote agent provider-inbound to diagnostics, contingent on
  evidence that the outbound track supplies frame coverage outside active
  playback.

- 2026-07-12, authorized iterative attempt 23 (`target_legs=opposite`, revision
  `ef4e3ea`): **NO-GO — `media_format_mismatch`**. The first monitor-topology
  call. Telnyx accepted the second, receive-only stream on the agent leg — the
  attempt's primary open unknown — and delivered its authenticated start about
  600 ms after the bridge. The sanitized start metadata showed the monitor
  stream was L16/16 kHz/mono despite the PCMU transcode request, and the exact
  PCMU gate failed closed before the agent greeting, any media capture, or
  fixture arming. Both legs were hung up, exact agent-webhook restoration was
  verified, and the tunnel was stopped. Determination: the agent leg's
  receive-only stream inherits the leg's native L16 media context and does not
  honor codec overrides, unlike the PSTN probe leg. The monitor gate was
  corrected offline to exact L16/16 kHz with an exact 640-byte outbound
  framing gate and no normalization; channel B gains native-rate fidelity.
  No detector threshold, time bound, or promotion gate was changed.

- 2026-07-12, authorized iterative attempt 24 (`target_legs=opposite`, revision
  `d42e3b8`): **NO-GO — `agent_audio_not_observed`**. All three sockets passed
  their exact format gates, including the corrected native-L16 monitor gate.
  Channel A again carried the delivered greeting cleanly (76 active frames
  spanning the injection window), reproducing attempt 22's channel-A evidence.
  The monitor's provider-outbound track delivered zero frames for the entire
  run, so measured channel B had no coverage, joint greeting selection could
  not complete, and the run failed closed at the unchanged 15-second horizon
  with only the channel-A diagnostic WAV written. The captured metadata
  resolved two structural questions: the probe leg's inbound track also had
  zero frames — the Call Control-answered harness endpoint transmits no RTP,
  so nothing is ever bridged toward the agent leg and its delivery-gated
  outbound track has nothing to send — and the monitor's inbound track carried
  the same injected-greeting mirror as the bidirectional socket's inbound
  (identical active window and peak), proving the mirror belongs to the agent
  leg's media rather than to the bidirectional socket. No fixture was armed.
  Both legs were hung up, exact webhook restoration was verified, and the
  tunnel was stopped. The caller-line keepalive correction described above was
  then prepared for review.

- 2026-07-12, authorized iterative attempt 25 (`target_legs=opposite`, revision
  `a6cc1f7`): **NO-GO — `media_format_mismatch`**. The first caller-line
  keepalive call. The agent, monitor, and probe sockets all validated their
  exact formats, but the keeper socket's StartFrame failed its L16 gate before
  injection began: the probe leg reports its PCMU media context on an attached
  stream's start, and the keeper's gate had assumed the bidirectional L16
  request would be reflected there. That start format describes only the
  keeper's discarded inbound track — the injection format is fixed by the
  explicit `stream_bidirectional_*` request — so gating it was unnecessary
  strictness on an unmeasured surface. No fixture was armed; both legs were
  hung up. The keeper now records the observed start format sanitized without
  gating it, and injection begins on StartFrame receipt. No measured-channel
  gate was weakened.

- 2026-07-12, authorized iterative attempt 26 (`target_legs=opposite`, revision
  `875882e`): **NO-GO — `agent_audio_not_observed`**. The caller-line
  keepalive operated as designed: the keeper connected, reported the expected
  PCMU leg context sanitized, and injected paced true silence for the whole
  run — the probe leg's diagnostic inbound carried its entry mirror
  continuously. Two determinations followed. First, the monitor's outbound
  track still delivered zero frames despite continuous caller-line audio
  delivered toward the agent leg. Second, the probe leg's measured
  receive-only outbound track collapsed from hundreds of frames in attempts
  22 and 24 to a single frame, extending the attempts-10/11 finding:
  attaching a bidirectional stream to a leg suppresses outbound-track
  delivery for every stream on that leg. No fixture was armed. Both legs were
  hung up, exact webhook restoration was verified, and the tunnel was
  stopped. This empirically rejects the caller-line keepalive and, with it,
  the last plan-compatible Call Control media-streaming topology.

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
- [x] One authorized receive-only PCMU-request attempt retained.
- [x] One authorized PCMU-negotiated framing attempt retained.
- [x] One authorized receive-only common-start attempt retained.
- [x] One authorized diagnostic-inbound framing attempt retained.
- [x] One authorized receive-only returned-greeting prerequisite retained.
- [x] One authorized full-transport attempt retained.
- [x] One authorized unchanged full-transport retry retained.
- [x] One authorized whole-buffer full-transport retry retained.
- [x] One authorized opposite-target full-transport attempt retained.
- [x] One authorized monitor-topology format attempt retained.
- [x] One authorized monitor-coverage attempt retained.
- [x] One authorized keeper-format attempt retained.
- [x] One authorized caller-line keepalive attempt retained.
- [x] Manual waveform agreement (SIP attempt 5, recorded 2026-07-13).
- [ ] Completed bounded calibration.
- [ ] Stable two-track live capture and separation.
- [x] Independent sanitized-evidence review confirms empirical NO-GO.

Phase 3 must not begin while any item remains unchecked. A live failure is
`NO-GO`, not permission to weaken the endpoint. `measurement_profile.json` may
be created only after a genuine, manually confirmed, independently reviewed
live GO.

## SIP harness live attempts (Amendment 1)

These attempts ran the external SIP media-endpoint harness of
`BENCHMARK_PLAN.md` Amendment 1 against the unchanged production runtime,
under the user's explicit per-attempt authorization. Their sequencing ahead
of Amendment 1's final review sign-off is disclosed in the amendment's
addendum. Artifacts are local and ignored; none are promotion evidence.

- 2026-07-12, SIP attempt 1 (revision `2e42ee2`, artifact
  `p2-1b1c880bf35ff346`): **NO-GO — `media_ordering_anomaly`**. First call
  through the SIP harness: answered with exact PCMU, anchored the greeting,
  confirmed the natural stop, recorded the emission boundary, completed
  fixture transmission, and confirmed separating silence — every stage the
  Call Control surface never reached. Failed closed at 191 ms of tx cadence
  drift caused by the harness itself: full-history PCM re-analysis and the
  Window D correlation ran on the media path. The agent transcribed the
  fixture and responded. pjsua2 teardown aborted the process after
  artifacts were flushed; the hangup reached the far end late.
- 2026-07-12, SIP attempt 2 (revision `c5032bf`, artifact
  `p2-1c0f45efeed1207d`): **NO-GO — `media_ordering_anomaly`**. With
  analysis moved off the media path, residual per-frame Python overhead
  (pure-Python G.711, per-row file opens, per-frame SWIG loops) still
  drifted 295 ms by frame 388, before the greeting anchored. Fixed offline
  with C-speed conversion paths (equivalence test-pinned), batched metadata
  flushes, cached tx buffers, and a burst-based pull gate that records
  lateness as telemetry.
- 2026-07-12, SIP attempt 3 (revision `f438168`, artifact
  `p2-25cedeecf3bd1cc7`): **NO-GO — `post_stimulus_response_not_observed`**.
  The media clock ran perfectly (zero max lateness). Greeting, stop,
  emission, transmission, and separating silence all confirmed; the agent
  transcribed the fixture and spoke a genuine response — but the Window D
  echo gate's length requirement was keyed to the latest activity run,
  which real speech pauses kept resetting, so the judging window never
  filled and the hard cap expired. Fixed offline with a sticky
  first-activity anchor; replaying this attempt's own recorded rx yields
  `capture_complete_pending_review`.
- 2026-07-12, SIP attempt 4 (revision `adc1f48`, artifact
  `p2-962141c3d5d56703`): **NO-GO — `rx_timeline_discontinuity`**. All
  stages through separating silence confirmed again, with clean tx timing
  and zero RTP anomalies except the failing event: one lost packet of 521
  during the response window, fatal under the first review's
  any-in-window-loss rule. This attempt motivates the bounded loss-void
  addendum; its artifact is the addendum's cited evidence.

- 2026-07-13, SIP attempt 5 (revision `1056e28`, artifact
  `p2-96ddebf9e4d6eb36`): **CAPTURE_COMPLETE_PENDING_REVIEW** — the first in
  the project across all thirty-one live attempts. The run also validated
  the loss-void addendum by fire: one packet of 1,244 was lost during the
  greeting window, its 191 ms poll interval was voided and recorded with
  rx-sample bounds, and the natural-stop hold restarted past the void
  rather than certifying across it. Every stage then completed: Window A
  anchor, natural stop, stimulus emission boundary (tx sample 98,764),
  verified transmission, separating silence, and a Window D response judged
  not-an-echo, with zero tx pull lateness and no RTP anomalies. The agent
  transcribed the fixture and injected a second-generation response. Both
  ends hung up, the webhook was restored and verified, and the tunnel and
  server were stopped. Per the promotion gates this is NOT a GO: manual
  waveform agreement (with the void overlay), fixture-correlation review,
  bounded detector calibration, the plan §7 SIP-topology calibration
  captures, and an independent evidence review all remain before any
  `measurement_profile.json` freeze.

### Manual waveform agreement — SIP attempt 5

Recorded 2026-07-13. The maintainer reviewed run `p2-96ddebf9e4d6eb36`
against an annotated evidence rendering (two-channel waveforms on one host
clock with Window A–D bands, the voided interval overlaid at its recorded
sample bounds, boundary insets, detector window summaries, fixture-envelope
correlations, and the captured audio) and recorded **agreement** on every
item of the review procedure, including the void-aware additions below:

- Greeting appears only in Window A on rx (129 active frames, peak
  −7.6 dBFS) and ends in a genuine natural stop; the backdated boundary
  sits at the silence onset.
- tx is silent except the fixture in Window C; the transmitted audio's
  envelope correlation against the canonical fixture is 0.998.
- Window C contains zero active rx frames (no overlap, no observed echo of
  the transmission), followed by the ≥100 ms separating silence.
- The Window D response begins 7.65 s after the boundary and correlates
  0.80 against the fixture envelope — below the 0.85 threshold and
  indistinguishable from the greeting's different-speech baseline of 0.784,
  corroborating the not-an-echo judgment by ear as well as by number.
- The single 191 ms void sits mid-greeting, ends 1.12 s before the stop
  hold begins, and touches no certified interval; manifest void accounting
  matches the event log and sits within the declared bounds.
- Transport telemetry: zero tx pull lateness, zero RTP anomalies beyond
  the one voided loss (1 of 1,244 packets), RTT ≈ 80 ms.

The review rendering, its extracted data, and its generators are preserved
in the run directory under `review/` (local, ignored). Per the promotion
rules this agreement does not create a GO: the Section 7 calibration
captures, bounded detector calibration, and the independent evidence review
remain.

### Void-aware additions to the manual review procedure

For any run captured under the loss-void addendum, the manual waveform
review additionally requires: (1) overlay every `rx_void_intervals` entry
(host-time and rx-sample bounds from the manifest) on the rx waveform plot;
(2) verify each certified boundary's supporting run — Window A anchor
activity, the natural-stop hold, the separating silence, and the Window D
echo-judged span — lies entirely in verdicted, non-voided timeline;
(3) verify the manifest's void count and total duration match the
`rx_loss_interval_voided` events and sit within the declared bounds; and
(4) record the outcome of these checks in the sanitized agreement note.
