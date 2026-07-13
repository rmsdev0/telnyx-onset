# SIP media-endpoint harness specification

**Status:** draft accompanying `BENCHMARK_PLAN.md` Amendment 1 (2026-07-12).
Not implemented; pending methodology review. Nothing in this document weakens
a frozen detector, window, eligibility, or promotion rule.

## 1. Purpose

Replace the Call Control harness leg and provider media-stream capture with a
local SIP user agent that terminates call media itself. The harness becomes a
real caller: it transmits the caller line continuously, plays the fixture at
a locally timestamped instant, and records received audio — all in one
process on one monotonic clock. This satisfies the Phase 2 hard gate by
construction and removes provider stream topology from the measurement path.

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
        |  SIP/RTP (PCMU)                        ^ webhook + wss
        v                                        |
   Telnyx SIP credentials conn ----> PSTN/route ----> agent number
                                                  (agent Call Control app,
                                                   exactly as production)
```

- **Agent side is untouched.** The agent application, its temporary-webhook
  workflow, and the production `onset` runtime behave exactly as in live
  attempts 18–26. The bench no longer serves a `/webhook` for a harness leg,
  no longer starts probe/monitor/keeper streams, and needs no harness-side
  public endpoint of any kind.
- **Harness side.** A dedicated Telnyx **SIP Connection (credentials type)**
  with an **Outbound Voice Profile** (one-time portal setup). The harness
  authenticates with those credentials and places the call directly to
  `BENCH_AGENT_NUMBER`. Registration is unnecessary for outbound-only use.

## 3. Media path and formats

- Negotiated codec is pinned to **PCMU/8 kHz/mono** by offering only PCMU;
  any other negotiated codec fails closed as `media_format_mismatch`.
- **rx channel (returned agent audio):** every received, jitter-buffered
  20 ms frame is appended to `rx_8k.wav` (PCM16, 8 kHz, mono) in arrival
  order. Analysis applies the already reviewed G.711 zero-order-hold
  normalization to form the 16 kHz analysis timeline; boundary resolution
  remains the 8 kHz sample period, exactly as in attempts 15–26.
- **tx channel (stimulus reference):** the transmit source is a
  deterministic frame generator, not a live capture: continuous digital
  silence frames, then the fixture's exact PCM (downmixed from the canonical
  16 kHz fixture to 8 kHz by the declared decimation rule and PCMU-encoded
  for transmission), then silence again. The generator writes each frame it
  hands to the SIP stack to `tx_8k.wav` and records, on the process
  monotonic clock, the handoff instant of the first fixture frame containing
  the fixture's known first active sample. That instant is the stimulus
  emission boundary. The canonical 16 kHz fixture, its SHA-256, and its
  first-active-sample onset metadata are unchanged.
- Both WAVs advance on the same media clock; rx/tx sample indexes are
  mapped to host monotonic time through per-frame timestamp records kept in
  `frame_metadata.jsonl` (RMS dBFS, peak, clipping, sample offset — same
  sanitized schema as today, no raw PCM in metadata).

## 4. Control loop (staged, fail-closed)

State machine mirrors the probe controller, with the same named categories:

1. `dial_requested` → SIP INVITE. Failure/timeout → `dial_failed`.
2. Answer + codec check (PCMU exact) → `media_format_validated`; the caller
   line begins transmitting silence immediately. No RTP within the state
   timeout → `stream_start_failed`.
3. **Window A/B:** run the frozen detector candidate over rx (16 kHz
   normalized timeline): greeting activity, then natural stop
   (500 ms sustained silence). Horizon and thresholds unchanged
   (15 s greeting horizon anchored to first greeting activity on rx; the
   agent's causal `greeting_output_started` event is no longer observable at
   the harness and is not required — Window A is defined on rx activity, as
   the plan's harness-boundary metric always specified). No qualifying stop →
   `agent_audio_not_observed` / `natural_stop_not_observed`.
4. **Window C:** splice the fixture into tx at the next frame boundary;
   record the emission boundary; return to silence after the final fixture
   frame. Then require ≥100 ms of separating silence on both channels (tx is
   silent by construction; rx must show no overlapping activity —
   `stimulus_overlap` otherwise).
5. **Window D:** genuinely new rx activity after the boundary →
   `CAPTURE_COMPLETE_PENDING_REVIEW`; horizon expiry →
   `post_stimulus_response_not_observed`.
6. Teardown: SIP BYE, recorder flush, manifest write. Hard 60 s call cap and
   single-attempt cap unchanged.

Cross-feed control: rx activity during fixture transmission is a true echo
observation (there is no provider mirror in this path). The existing
contamination analysis runs unchanged over (rx, tx).

## 5. Reused components (unchanged)

- Detector candidate structure, thresholds, windows, sustained-silence rule.
- Fixture loader, hash/onset validation, all-silence rejection.
- G.711 decode/normalization and its known-vector tests.
- Energy-envelope fixture correlation (including the PCMU-transcode
  correlation test added after attempt 24).
- Manifest schema (git commit, dirty tree, detector candidate, fixture
  onset, per-channel `measured_media_format`, `channel_sources` =
  `{channel_a: harness_rx, channel_b: harness_tx_reference}`), sanitized
  events/frame-metadata, artifact directory rules (`bench/artifacts/p2-*`,
  0700/0600, gitignored), failure taxonomy.

## 6. Dropped components (measurement path only)

Probe/monitor/keeper websocket routes and streams, stream tokens, per-call
webhook override, bridging, the one-time HTTPS fixture transport (the fixture
is now emitted directly by the harness), and the cross-socket 40 ms skew
machinery (single-process capture makes it moot; the manifest records the
per-frame clock mapping instead). All remain in history; the agent-side
websocket auth for the production media socket is unchanged.

## 7. Dependencies and configuration

- **Dependency:** `pjsua2` Python bindings (PJSIP ≥ 2.14), built locally or
  via a pinned container. The SIP stack is bench-only and must not be
  imported by production `onset` modules.
- **New ignored `.env` values:** `BENCH_SIP_USERNAME`, `BENCH_SIP_PASSWORD`,
  `BENCH_SIP_DOMAIN` (`sip.telnyx.com`). Existing agent-side values are
  unchanged. Credentials never appear in artifacts or logs.
- **One-time portal setup (user action):** create the credentials SIP
  connection, attach an Outbound Voice Profile, confirm PCMU is permitted.
- The `--target-legs` flag, being a Call Control streaming concern, does not
  exist in this harness; the CLI grows `--live`/`--fixture` only.

## 8. Offline validation strategy

The SIP stack is isolated behind a thin `CallMedia` interface (place call,
codec result, frame source hook, frame sink hook, hangup). Offline tests
drive the control loop with an in-memory fake implementing that interface:
staged greeting/stop/fixture/response scenarios, overlap and echo
adversarial cases, teardown on every failure category, artifact
sanitization, and WAV/metadata integrity — to the same standard as the
existing suite. pjsua2 itself is exercised only in the bounded live attempt.

## 9. Risks and open questions for review

1. **Jitter-buffer timing:** rx frame arrival time is post-jitter-buffer;
   the manifest must record the configured jitter buffer bound and the
   detector continues to operate on sample timelines, not arrival times.
2. **Fixture decimation:** 16 kHz → 8 kHz reduction rule (drop-every-other
   vs. proper low-pass) must be declared and frozen before qualification;
   the correlation threshold evidence from the transcode test applies.
3. **Echo exposure:** with a real transmit path, provider-side echo of tx
   into rx becomes observable; the no-stimulus control calls (plan §13)
   quantify it before any interpretation.
4. **pjsua2 build reproducibility:** pin version and record it in the
   manifest with the Python and OS versions.
