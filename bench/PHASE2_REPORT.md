# Phase 2 acoustic-boundary probe report

## Verdict

**CONDITIONAL GO**

The isolated implementation and offline validation pass. The empirical Phase 2
gate is not complete because no live probe was authorized or run: the required
`--live` invocation was absent and the `BENCH_LIVE`, trusted-number, and public
WSS configuration gates were not present. This report contains no comparative
latency result.

## Repository state

- Implementation base: `5b8da341e0db3d279b5767311cedf870a977f249`
- Branch: `duplex`
- Phase 1 pre-registration: committed and unchanged
- Working tree: dirty only with the uncommitted Phase 2 files listed below
- Production runtime modules modified: none

## Objective

Prove or disprove that one controlled harness process can emit a known local
synthetic caller fixture and capture returned agent audio on one host monotonic
clock, with stable track separation, bounded timing uncertainty, and no
material fixture contamination on the selected agent track.

## Implemented topology

`bench.acoustic_probe` is a separate FastAPI application and entrypoint. During
an explicitly gated live run it:

1. Dials the configured agent number from the configured harness number.
2. Owns the returned outbound call-control ID as leg A, in memory only.
3. Accepts one matching incoming call as leg B and answers it.
4. Starts an authenticated `both_tracks`, bidirectional L16/16 kHz RTP stream on
   leg A with an explicitly selected `self` or `opposite` target.
5. Starts the normal production media path and an unmodified `VoiceAgent` on
   leg B through the bench app's separate agent WebSocket route.
6. Bridges the legs after both are answered.
7. Captures the agent greeting and natural stop before pacing the fixture.
8. Requires a post-stimulus agent response and stop.
9. Explicitly attempts to hang up both legs in all teardown paths.

The production `onset.server` dispatch and production decoder were not changed.

## Files and commands

Implemented files:

- `.gitignore`
- `bench/__init__.py`
- `bench/acoustic_probe.py`
- `bench/media_capture.py`
- `bench/acoustic_stop.py`
- `bench/PHASE2_REPORT.md`
- `tests/test_bench_acoustic_probe.py`
- `tests/test_bench_media_capture.py`
- `tests/test_bench_acoustic_stop.py`

Validation commands:

```text
.venv/bin/pytest -q
.venv/bin/ruff check onset/ bench/ tests/
.venv/bin/mypy bench/ tests/test_bench_*.py
.venv/bin/mypy onset/ bench/ tests/
git diff --check
git check-ignore -v bench/artifacts/
```

## Authentication and webhook safety

- Stream tokens use 32 random bytes, are scoped to run and call-control ID,
  expire after 60 monotonic seconds, are one-use, and live in a bounded store.
- Comparisons use `hmac.compare_digest`.
- Tokens are accepted only from the documented WebSocket header or connected
  frame field.
- The token is sent using `stream_auth_token`, never in the stream URL.
- Capture allocation occurs only after authentication.
- The start-frame call-control ID must match the authorization.
- Webhooks retain Ed25519 verification and a 256 KiB body ceiling.
- Duplicate webhook IDs are acknowledged without repeating side effects.
- Raw bodies, phone numbers, provider IDs, tokens, and transcripts are not
  written to artifacts.

## Fixture

The live fixture loader requires mono, uncompressed PCM16 WAV at 16 kHz, at
most 1 MiB and 10 seconds. It records the container SHA-256, never the source
path. It finds the first sample whose absolute PCM16 value reaches the declared
threshold and records its sample/frame offset. All-silence input is rejected.

No live fixture was supplied, so this report has no live fixture hash.

## Media format

Requested live format: L16, 16 kHz, mono, 20 ms frames; leg A requests
`both_tracks`. The decoder rejects mismatched codec, rate, or channels and does
not resample or coerce them.

Observed live format: not available because no call ran.

## Track mapping and contamination

Track labels are never hardcoded as agent or stimulus. The planned mapping uses
an agent-only greeting, confirmed natural silence, stimulus-only playback, and
post-stimulus response window. Exactly one track must contain the greeting and
natural stop; both tracks must be present and stable.

The stimulus-only check records agent/stimulus mean-square energy ratio and
zero-lag absolute waveform correlation. Candidate limits are an energy ratio of
at most 0.10 and absolute correlation of at most 0.80. These are Phase 2
diagnostic candidates, not a frozen Phase 3 measurement profile. Mixed tracks
fail offline tests. No live track mapping or contamination finding exists yet.

## Pacing

PCM is split into 320-sample/640-byte frames. Only the final frame may be
zero-padded. Deadlines use:

```text
deadline_n = origin_monotonic_ns + n * 20 ms
```

Each frame records deadline, send-start, send-complete, lateness, and byte
count. A live run fails if any frame exceeds the predeclared 10 ms scheduling
tolerance. Fake-clock tests confirm absolute deadlines, padding, diagnostics,
and cancellation behavior. No live pacing values exist yet.

## Ordering and capture integrity

The strict decoder retains arrival time, sequence, track, chunk, timestamp, and
decoded PCM while keeping provider identifiers in memory. Ordering state is
independent by track and records duplicates, sequence regressions, chunk gaps,
chunk regressions, and timestamp regressions. Any unresolved anomaly prevents a
GO. Primary timing uses arrival order; no silent reconstruction is performed.

Offline ordered/anomalous cases pass. No live ordering finding exists yet.

## Detector validation

The pure detector uses RMS dBFS over exact PCM16 windows. It requires prior
continuous activity, tolerates pauses shorter than the sustained-silence hold,
confirms the complete hold, and backdates the result to the first silent window.
It reports window resolution and clipping and rejects odd-byte PCM.

Offline cases passing include all silence, continuous activity, active-to-stop,
short and multiple natural pauses, abrupt cut, fade-out, low noise, noise above
threshold, exact hold boundary, partial final window, clipping, minimum signed
PCM, malformed input, and absent prior activity.

Live natural-stop validation: not run. Offline abrupt-stop validation: passed.
Live abrupt-stop validation is deferred because Phase 2 does not modify
VoiceAgent, enable unsafe full duplex, or use transcript-race behavior.

## Manual spot-check procedure

After a bounded live run, inspect only the ignored local run directory:

1. Compare `agent_track.wav` and `stimulus_track.wav` across greeting,
   silence, stimulus, and response windows.
2. Compare the visible boundary with `energy_by_frame.csv` and
   `detector_summary.json`.
3. Confirm the selected agent track is quiet during stimulus-only playback and
   newly active afterward.
4. Confirm no unexplained discontinuity aligns with either detected stop.
5. Record agreement or disagreement in a sanitized revision of this report.

The manual check is waveform agreement validation, not human-perception
measurement.

## Resolution and uncertainty

The candidate stop has detector-window resolution and retains the receive time
of its containing frame. Stimulus onset is bounded by first-frame send-start,
send-complete, and the known first-active sample offset. Reportable uncertainty
includes one 20 ms frame, send duration, scheduling lateness, receive batching,
within-frame offset, and ordering anomalies. Provider timestamps are integrity
metadata and are never subtracted from host monotonic timestamps.

## Security and privacy checks

- `bench/artifacts/` is narrowly ignored; the rule was verified with
  `git check-ignore`.
- Run IDs and artifact filenames are internally generated and path-checked.
- Run directories use mode `0700`; files use `0600`.
- Capture is bounded to 4 MiB per track and 20,000 frame rows.
- Calls and captures are capped at 60 seconds, with named state timeouts.
- The code permits at most three configured attempts and one controller/run.
- Live execution requires both `--live` and `BENCH_LIVE=1`.
- Live configuration is rejected unless the existing safe half-duplex
  listening policy remains enabled.
- The live CLI reruns ignore verification, pytest, Ruff, and mypy before
  starting the server or dialing.
- No live audio, JSONL, tokens, numbers, or provider IDs were created.

## Offline check results

- `pytest`: **108 passed**, one third-party Starlette deprecation warning.
- Phase 2 tests: **47 passed**.
- Ruff across `onset/`, `bench/`, and `tests/`: **passed**.
- Strict mypy for all new Phase 2 modules/tests: **passed**.
- Repository-wide mypy: four unchanged pre-existing errors in
  `tests/test_media.py` and `tests/test_tts.py`; neither file was modified.
- `git diff --check`: **passed**.
- Artifact ignore verification: **passed**.

## Remaining live unknowns

- Actual `self`/`opposite` behavior on this bridged topology.
- Actual track-to-leg orientation.
- Stability and separation of both tracks.
- Actual negotiated media format and frame characteristics.
- Stimulus loopback/cross-feed.
- Live pacing and receive jitter.
- Live frame ordering and gap behavior.
- Natural-stop agreement with manual waveform inspection.
- Whether the synthetic fixture produces distinguishable post-stimulus agent
  audio.

## Gate checklist

- [x] Isolated bench-only application.
- [x] Production VoiceAgent unchanged.
- [x] Probe-specific strict decoder.
- [x] Same-process injected monotonic clock.
- [x] Bounded one-use stream authentication.
- [x] Signed and bounded webhook path.
- [x] Bounded fixture, capture, events, attempts, call, and teardown.
- [x] Offline fixture, pacing, ordering, detector, privacy, and controller tests.
- [x] Offline abrupt-stop waveform validation.
- [ ] Deterministic live stimulus injection.
- [ ] Stable live media format.
- [ ] Defensible live agent-track mapping.
- [ ] No material live contamination.
- [ ] No unresolved live ordering anomaly.
- [ ] Live natural-stop/manual-waveform agreement.
- [ ] Live post-stimulus agent response.

Phase 3 must not begin until every unchecked item passes and this report is
updated to **GO**. A live **NO-GO** must leave the preregistration unchanged
pending a separately reviewed amendment.

## Measurement profile

`bench/measurement_profile.json` was deliberately not created. The methodology
allows it only after a live GO freezes the observed format, track mapping,
detector settings, contamination criterion, and ordering tolerance.
