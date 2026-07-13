# SIP media-endpoint harness specification

**Status:** revision 2, accompanying `BENCHMARK_PLAN.md` Amendment 1
(2026-07-12). Revision 2 incorporates all twenty-eight findings of the first
independent methodology review (plan consistency, measurement validity,
evidence audit, security/operations, implementability). Not implemented;
pending final methodology review. Nothing in this document weakens a frozen
detector threshold or window definition; every disclosed rule change is
listed in Amendment 1's "What changes."

## 1. Purpose

Replace the Call Control harness leg and provider media-stream capture with a
local SIP user agent that terminates call media itself. The harness becomes a
real caller: it transmits the caller line continuously, emits the fixture at
a locally timestamped instant, and records received audio — one process, one
monotonic clock. This satisfies the Phase 2 hard gate directly and removes
provider stream topology from the measurement path.

## 2. Architecture

```
+---------------------------- bench host ----------------------------+
|                                                                     |
|  sip_harness (new, bench-only)          onset server (production)   |
|  - pjsua2 SIP UA, PCMU pinned           - unchanged VoiceAgent      |
|  - dials agent number via SIP           - L16/16k bidirectional     |
|    credentials connection                 media websocket           |
|  - tx: caller line + fixture            - webhook via tunnel        |
|  - rx: recorded to WAV                    (agent app only)          |
|  - control loop + artifacts                                         |
+---------------------------------------------------------------------+
        |  SIP (TLS) / RTP (PCMU)                ^ webhook + wss
        v                                        |
   Telnyx SIP credentials conn ----> route ---------> agent number
                                                  (agent Call Control app,
                                                   as in attempts 22-26)
```

- **Agent side is untouched.** The agent application, its temporary-webhook
  workflow, and the production `onset` runtime are exactly as configured
  from live attempt 22 onward: whole-buffer bench greeting decode (in force
  from attempt 21) and `stream_bidirectional_target_legs=opposite` (in force
  from attempt 22). The bench no longer serves a harness `/webhook`, starts
  no probe/monitor/keeper streams, and needs no harness-side public endpoint.
- **Harness side.** A dedicated Telnyx **SIP Connection (credentials type)**
  with an **Outbound Voice Profile** and an owned caller-ID number
  (`BENCH_SIP_CALLER_ID`); one-time portal setup. Outbound-only: no
  REGISTER. SIP signaling uses TLS to `sip.telnyx.com`.
- **Inbound posture.** The UA processes no unsolicited traffic: any incoming
  call or out-of-dialog request on the harness transport is rejected
  immediately without processing. The SIP transport and RTP sockets bind to
  a declared local interface with a bounded, documented port range, and RTP
  is source-filtered to the negotiated peer where the stack supports it.
- **Routing verification (preflight item).** A credentials-connection call
  to `BENCH_AGENT_NUMBER` may route on-net; the first bounded attempt must
  confirm it reaches the agent's Call Control application through the same
  inbound path observed in attempts 22–26 before any result is interpreted.

## 3. Media path and formats

### Codec and negotiation

- Offer PCMU only; a negotiation result other than PCMU/8 kHz/mono fails
  closed as `media_format_mismatch`.
- Mid-call renegotiation (re-INVITE or UPDATE) that changes codec, payload
  mapping, or media direction fails closed as `media_format_mismatch` with a
  sanitized event; session-timer refreshes with unchanged SDP are accepted
  and logged. RFC 2833 telephone-event and comfort-noise payloads are
  excluded from the rx WAV and logged as sanitized counts; they never enter
  acoustic analysis.

### rx channel (returned agent audio)

- Recording begins at answer (200 OK); pre-answer early media (183) is
  discarded and its occurrence logged. Every received, jitter-buffered 20 ms
  frame is appended to `rx_8k.wav` (PCM16, 8 kHz, mono).
- The jitter buffer is pinned to a fixed depth (`jb_min == jb_max`);
  configured and observed delay are recorded in the manifest.
- `frame_metadata.jsonl` records, per rx frame: RTP sequence number, RTP
  timestamp, host receive monotonic time, RMS dBFS, peak, clipping count,
  and sample offset. The sample-index-to-monotonic mapping is defined by RTP
  timestamps anchored at the first received frame — never by arrival order.
- Loss, concealment, duplicate, or jitter-buffer resize events are counted;
  any such event inside Window C, the separating-silence interval, the
  sustained-silence stop region, or Window D fails closed as
  `rx_timeline_discontinuity` (new named category).
- Analysis applies the reviewed G.711 zero-order-hold normalization to form
  the 16 kHz analysis timeline (in force for PCMU capture from attempt 15
  onward; attempts 23 and 25 failed at format gates before media capture).
  Boundary resolution remains the 8 kHz sample period.

### tx channel (stimulus reference)

- The transmit source is a deterministic **pull-model** frame generator: the
  SIP stack's media clock requests each 20 ms frame (160 samples, 160-byte
  PCMU payload) via a clocked callback; no intermediate frame queue is
  permitted (fail closed if the stack requires one). Content: continuous
  digital silence, then the derived fixture, then silence.
- **Derived emission fixture.** The transmitted stimulus is produced from
  the canonical 16 kHz fixture by a frozen, declared anti-aliased decimation
  (group-delay-compensated low-pass below 4 kHz; drop-every-other decimation
  is explicitly rejected), then PCMU-encoded. The derived 8 kHz waveform is
  hashed (SHA-256), recorded as a first-class fixture artifact with its
  first-active-sample onset recomputed on the derived waveform, and frozen
  before qualification. The energy-envelope correlation evidence is rerun
  over the derived-plus-encoded waveform before qualification.
- **Emission boundary.** Authoritative on the tx sample index of the derived
  fixture's first active sample; the monotonic timestamp taken inside the
  frame-request callback that fills that frame is recorded alongside.
  Declared skew bound: one frame period (20 ms) plus encoder latency,
  verified once during qualification by comparing the callback timestamp
  against a packet-capture or transport-adapter timestamp of the first
  fixture RTP packet.
- Every frame handed to the stack is simultaneously appended to `tx_8k.wav`;
  cumulative handoff-cadence drift beyond half a frame period fails closed
  as `media_ordering_anomaly`.

### Cross-channel timing

All cross-channel temporal predicates — stimulus overlap, separating
silence, and Window boundaries — are evaluated in host monotonic time via
the per-frame timestamp records, never by WAV sample index. Successor to the
retired 40 ms cross-socket bound: the total mapping error budget (pinned
jitter-buffer depth plus the declared tx skew bound) is pre-registered as a
numeric bound in the manifest; a run whose observed timing telemetry exceeds
it fails closed as `rx_timeline_discontinuity`.

## 4. Control loop (staged, fail-closed)

### Declared parameters

| Parameter | Value | Note |
|---|---|---|
| Frame duration | 20 ms (160 samples, PCMU) | unchanged |
| Hard call cap | 60 s, armed before INVITE is sent | Setup time counts against the cap, as dialing did in attempts 1–26 |
| State timeout | 15 s per named state | restated normatively |
| Teardown timeout | 10 s | restated normatively |
| Greeting horizon | 15 s from the Window A anchor | unchanged |
| Sustained silence | 500 ms at –45 dBFS | unchanged |
| Activity threshold | –38 dBFS over 20 ms windows | unchanged |
| Separating silence | ≥ 100 ms on both channels | unchanged |

### Stages

1. `dial_requested` → INVITE. Transaction failure → `dial_failed`; ringing
   past the state timeout → `answer_timeout`.
2. Answer + exact PCMU validation → `media_format_validated`; caller-line
   silence transmission begins with the first frame-request callback. No rx
   RTP within the state timeout → `stream_start_failed`.
3. **Window A/B.** The Window A anchor is the first rx activity that
   satisfies a sustained-activity rule (a run of consecutive active 20 ms
   windows totalling ≥ 100 ms — the detector's existing `minimum_active_ms`)
   rather than any single active frame. Rationale and guard: this is the
   attempt-6 false-anchor failure mode; rx is recorded only from the
   answered, codec-validated dialog, early media is excluded, and isolated
   sub-threshold blips cannot anchor. The frozen detector then requires the
   natural stop (500 ms sustained silence). No qualifying activity within
   the horizon → `agent_audio_not_observed`; activity without a stop →
   `natural_stop_not_observed`.
4. **Window C.** The derived fixture is spliced into tx at the next frame
   boundary; the emission boundary is recorded; tx returns to silence after
   the final fixture frame. Delivery confirmation: SIP-stack transmit
   statistics and RTCP SR/RR counters covering the fixture interval are
   recorded in the manifest, and unaccounted transmit octets/packets fail
   closed as `stimulus_send_failed` (the trial is ineligible, preserving the
   plan's eligibility-independent-of-outcome rule). Then ≥ 100 ms separating
   silence on both channels; rx activity overlapping fixture transmission →
   `stimulus_overlap`.
5. **Window D.** Genuinely new rx activity after the boundary, subject to
   the echo-rejection rule: the energy-envelope fixture correlation is run
   against Window D rx activity over a declared delayed-alignment search
   range (0–4000 ms after emission), and a match at or above the frozen
   correlation threshold fails closed as `post_stimulus_echo_detected` (new
   named category). Only activity that does not match the fixture qualifies
   → `CAPTURE_COMPLETE_PENDING_REVIEW`; horizon expiry →
   `post_stimulus_response_not_observed`.
6. Teardown: BYE, recorder flush, manifest write — executed on every exit
   path, including exceptions and far-end teardown. A remote BYE or
   dialog-terminating error in any state maps to `call_hangup` unless a more
   specific category already fired; `teardown_result` distinguishes
   `remote_bye` from harness-initiated hangup success/failure.

### Process-independent call bounds

The control loop's cap timer is not the only bound: SIP session timers
(Session-Expires ≈ 90 s), the stack's RTP inactivity timeout, and a
Telnyx-side maximum call duration on the outbound voice profile (where
available) back-stop a harness crash mid-call. The configured backstops are
recorded in the manifest.

## 5. Metric composition statement

Harness-boundary interruption latency measured at this endpoint includes
both one-way media transits and the fixed, bounded harness receive-path
delay (pinned jitter buffer). Durations measured under this harness are
never pooled with or compared against provider-boundary or prior-topology
durations. A per-trial transit covariate (RTCP round-trip estimate) is
recorded alongside — never subtracted from — the headline metric.

## 6. Failure-taxonomy disposition

- **Retained unchanged:** `dial_failed`, `answer_timeout`,
  `stream_start_failed`, `media_format_mismatch`, `media_ordering_anomaly`,
  `agent_audio_not_observed`, `natural_stop_not_observed`,
  `stimulus_send_failed`, `stimulus_overlap`,
  `stimulus_boundary_ambiguous` (continuous rx activity after the fixture
  leaves no defensible Window C/D split), `post_stimulus_response_not_observed`,
  `capture_limit_reached`, `socket_error`, `call_hangup`, `teardown_timeout`.
- **Retired as structurally unreachable** (single empirical channel plus
  deterministic reference): `track_ambiguous`, `track_missing`,
  `fixture_match_missing`, `fixture_match_ambiguous`,
  `cross_channel_alignment_failed`, `stream_auth_failed`,
  `stream_call_id_mismatch`, `stimulus_transport_pending`.
- **New:** `rx_timeline_discontinuity`, `post_stimulus_echo_detected`.

## 7. Reused components (unchanged)

Detector candidate structure, thresholds, windows, and sustained-silence
rule; canonical fixture loader, hash/onset validation, all-silence
rejection; G.711 decode/normalization with its known-vector tests; the
energy-envelope correlation machinery (now used for delivery-era calibration
and the Window D echo-rejection gate; the PCMU-transcode correlation test
introduced with the monitor-topology correction at revision `ef4e3ea`, in
effect from attempt 23, applies directly to the derived emission fixture);
sanitized events/frame-metadata discipline; artifact directory rules
(`bench/artifacts/p2-*`, 0700/0600, gitignored); single-attempt and
hard-cap discipline.

## 8. Dropped components (measurement path only)

Probe/monitor/keeper websocket routes and streams, stream tokens, the
per-call webhook override, bridging, the one-time HTTPS fixture transport,
and the 40 ms cross-socket skew rule (superseded by the Section 3 mapping
error budget). All remain in history. The agent-side websocket
authentication for the production media socket is unchanged.

## 9. Security and privacy posture

- **SIP logging.** pjsua2 logging is configured so SIP message tracing is
  disabled (log level ≤ 2, message logging off); no pjsua2 log output is
  routed into run artifacts. An offline sanitization test asserts no SIP
  header material, username, credential digest, or phone number appears in
  any artifact file.
- **Transport.** SIP over TLS to `sip.telnyx.com` protects credentials and
  numbers in transit. SRTP is declared out of scope for media because every
  transmitted and received sample is synthetic benchmark audio per plan
  Section 17.
- **Recording posture (disclosed change).** rx/tx WAVs are captured on every
  attempt — a broader default than the old diagnostic-only exception — and
  this is acceptable because both channels contain only synthetic
  fixture/agent audio. The recorders enforce a hard sample-count bound
  derived from the 60 s cap plus margin, independent of the control loop;
  files are 0600 inside the 0700 gitignored run directory, writes are
  atomic/no-follow, and persistence can never block teardown. WAVs are
  diagnostic evidence, never committed and never sufficient for promotion.
- Credentials live only in ignored `.env` values and process memory.

## 10. Dependencies and configuration

- **Dependency:** `pjsua2` Python bindings (PJSIP ≥ 2.14), version pinned
  and recorded in the manifest with Python and OS versions; bench-only,
  never imported by production `onset` modules.
- **New ignored `.env` values:** `BENCH_SIP_USERNAME`, `BENCH_SIP_PASSWORD`,
  `BENCH_SIP_DOMAIN` (`sip.telnyx.com`), `BENCH_SIP_CALLER_ID` (an owned
  Telnyx number, preflight-checked against the outbound voice profile).
- **One-time portal setup (user action):** credentials SIP connection,
  outbound voice profile permitting the destination, PCMU permitted,
  caller-ID number attached, and (if available) a Telnyx-side maximum call
  duration.
- The `--target-legs` flag does not exist in this harness; the manifest
  field `target_legs` is recorded as `null` with the removal noted, because
  no Call Control streaming exists on the measurement path.

## 11. Manifest schema (normative)

Kept unchanged: `schema_version`, `run_id`, `git_commit`, `dirty_tree`,
`python_version`, `created_utc`, `clock`, `clock_process_id`,
`fixture_sha256` (canonical 16 kHz), `fixture_onset` (canonical),
`greeting_tts_decode_mode` (agent side unchanged), `capture_limits`,
`detector_candidate`, `gate_outcome`, `failure_category`,
`terminal_outcome`, `attempt_number`, `teardown_result`.

Changed: `channel_sources` = `{channel_a: harness_rx,
channel_b: harness_tx_reference}`; `measured_media_format` = PCMU/8 kHz with
ZOH analysis rate on both channels; `target_legs` = `null` (removal noted).

New required fields: `emitted_fixture_sha256`, `emitted_fixture_onset`,
`decimation_rule`, `jitter_buffer_ms` (configured and observed),
`tx_skew_bound_ms`, `timing_error_budget_ms`, `rtp_rx_counters` (loss,
concealment, duplicates, resizes), `tx_delivery_counters` (stack TX and
RTCP), `rtt_estimate_ms`, `session_backstops`, `pjsua2_version`,
`os_version`, `sip_transport` (`tls`).

## 12. Offline validation strategy

The SIP stack is isolated behind a `CallMedia` interface exposing: place
call (with codec result), a **clocked pull callback** for tx frames driven
by an injected `MonotonicClock`, an rx frame sink carrying RTP
sequence/timestamp metadata, mid-call event callbacks (re-INVITE, BYE,
DTMF/CN), and hangup. Offline tests drive the control loop with in-memory
fakes to the existing suite's standard: staged greeting/stop/fixture/
response scenarios; the attempt-6 false-anchor scenario (setup blip before
the real greeting must not anchor Window A); a deliberately skewed fake
clock that fails if any cross-channel predicate is evaluated by sample
index; echo-injection cases that must trigger `post_stimulus_echo_detected`;
concealment/loss cases that must trigger `rx_timeline_discontinuity`;
remote-BYE in every stage; teardown on every failure category; inbound-call
rejection; and artifact sanitization (no SIP material, credentials, or
numbers). pjsua2 itself is exercised only in the bounded live attempt.

## 13. Calibration prerequisites (before any detector freeze)

Per plan Section 7 under the SIP topology: no-stimulus control calls
(Section 13 of the plan), **stimulus-echo control calls** (fixture
transmitted against a silent or non-agent far end; any rx return measured
and recorded — the no-stimulus controls are structurally blind to tx echo),
and agent-only baseline captures. Results are recorded before any
interpretation of Window D activity.

## 14. Residual risks

1. pjsua2 build reproducibility (pinned version; container fallback).
2. On-net routing versus PSTN routing for a same-account call (Section 2
   preflight verification item).
3. Provider-side echo characteristics unknown until the calibration calls
   run; the Window D echo-rejection gate is the fail-closed guard.
4. Fixed jitter-buffer depth trades adaptivity for timing determinism; the
   chosen depth is recorded and its adequacy reviewed after the first
   bounded attempt.
