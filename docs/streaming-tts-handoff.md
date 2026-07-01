# Handoff: streaming TTS decoder (scope, then plan)

## Mission

Scope and produce an implementation plan for a **streaming / incremental MP3
decode** path in the Telnyx media-stream voice agent, to cut time-to-first-audio
while keeping playback gap-free. This task is **scope then plan only**. Produce a
reviewed plan and STOP. Do not write the decoder/loop until the plan is approved.

## Where things are

- Repo: `/Users/rschuetz/Code/telnyx-onset` (Python 3.12, `uv`, package `onset/`,
  venv at `.venv`).
- Files that matter:
  - `onset/tts.py` — the Telnyx TTS WebSocket client. **This is the file to
    change.** `TtsClient.synthesize(text)` currently buffers the whole MP3 then
    decodes it.
  - `onset/agent.py` — `_speak()` is the consumer: it opens an utterance epoch,
    iterates `synthesize()` frames into `media.send_audio_frame(epoch, frame)`,
    sends a mark fence, and waits for the mark echo. It also owns barge-in
    cancellation, `spoken_tokens` rollback, and the `latency.first_audio` log.
  - `onset/media.py` — `MediaStream`: the 20 ms outbound pacer, the bounded
    lead queue, the epoch guard, `flush()` (the barge-in `clear`), and the mark.
  - `onset/audio.py` — `decode_mp3_to_pcm16(mp3, sample_rate)` (miniaudio,
    whole-buffer), `frame_pcm16(pcm, frame_bytes)`, `decode_l16`/`encode_l16`.
  - `onset/settings.py` — `sample_rate=16000`, `frame_ms=20`, `frame_bytes`,
    `tts_voice`, `inject_lead_frames`.
- Read first: `DISCOVERY.md` (architecture; §13 has the live socket findings),
  `README.md`.
- Offline TTS tooling: `scripts/probe.py` (socket protocol probe). Model the
  required validation script on it.
- Credentials: `.env` (gitignored) holds `TELNYX_API_KEY`. Source it without
  echoing, e.g. `export TELNYX_API_KEY="$(grep -E '^TELNYX_API_KEY=' .env | cut -d= -f2-)"`.
- Git: PR #1 (core loop) is merged to `main`; PR #2 (docs) is open on
  `chore/publication-prep`. Do the streaming work on a **fresh branch off `main`**.

## The problem (precise)

- The agent talks to the Telnyx TTS WebSocket
  (`wss://api.telnyx.com/v2/text-to-speech/speech?voice=Telnyx.NaturalHD.astra`).
  It returns base64 **MP3** chunks (16 kHz) with `isFinal` on the last. MP3 is the
  only output (probed: no raw/PCM option).
- `tts.synthesize()` today **accumulates every MP3 chunk until `isFinal`, then
  decodes the whole buffer** (`decode_mp3_to_pcm16` via miniaudio, off-loop in a
  thread) **and yields 20 ms L16/16k frames**, which `_speak` paces into the call.
- In-call, Telnyx streams the MP3 at roughly **real time**, so waiting for the
  whole reply's MP3 makes time-to-first-audio scale with reply length: measured
  **3.7 to 10.5 s** on real calls (`latency.first_audio`).
- The standalone probe shows the **first MP3 chunk arrives in ~0.5 to 1 s**. The
  current code throws that head start away by waiting for `isFinal`.

### A dead end already tried and reverted (do not repeat)

Per-sentence synthesis (split the reply, synthesize+play each sentence, pipelined
so sentence N+1 synthesizes while N plays) was implemented and **reverted**. It
barely cut first-audio (the first sentence is still a full ~3 s sentence) and it
introduced **~2 s gaps between sentences**: each sentence's full-MP3 synth (~real
time) finishes after the previous sentence's playback ends, so the pacer starves.
Net result was choppy and worse. **Chunking-by-sentence-with-full-chunk-buffering
cannot win.** The only known fix is decoding within the stream.

## The goal

Time-to-first-audio ≈ the first MP3 chunk (~0.5 to 1 s) **and** gap-free playback.
The known approach: **decode the MP3 incrementally and emit L16 frames as chunks
arrive** (over one socket per turn), so playback keeps pace with the real-time
MP3 stream instead of waiting for the whole reply.

## Hard constraints (must hold)

- The outbound seam is unchanged: 20 ms L16/16k frames into
  `media.send_audio_frame(epoch, frame)`. Pacing, the epoch guard, `flush()`
  (barge-in `clear`), and the mark fence (playback-complete) live in
  `MediaStream` + `_speak`. Do not break: the epoch guard, the mark fence,
  barge-in cancel + flush, or the half-duplex gate.
- MP3 to PCM16/16k stays the one and only transcode in the loop.
- Quality bar: `mypy --strict` clean, `ruff` clean, fully typed, Python 3.12.
  Tests use fakes with **no network**. **No em dashes anywhere.**
- One Telnyx API key; do not add a vendor or a heavy dependency without
  justifying it against the alternatives.

## What to produce (scope + plan, no loop code)

1. **Decoder options, evaluated with tradeoffs.** At least:
   - miniaudio streaming (it is already a dependency): `stream_any` /
     `StreamableSource` / its streaming decoders fed from the WS, or repeated
     decodes of the growing buffer.
   - PyAV / ffmpeg incremental decode (NOT currently a dependency: weigh the
     packaging cost).
   - A minimal MP3-frame parser that decodes whole MP3 frames as they complete.
   For each: correctness at chunk boundaries (MP3 bit-reservoir / frame
   alignment), how to bridge a **sync decoder** with the **async WS recv** loop
   (thread vs incremental vs `to_thread` per chunk), CPU / event-loop impact
   under concurrent media + STT I/O, dependency weight.
2. **A recommended design.** Data flow (WS chunk -> decoder -> L16 frames ->
   `send_audio_frame`), where decode runs, how leftover/partial bytes are carried
   between chunks, resampling if the engine is not exactly 16 k, and how it slots
   into `_speak` (the `latency.first_audio` log, the mark fence, `spoken_tokens`,
   barge-in cancellation cleanup, and the per-recv timeout `_RECV_TIMEOUT_S`,
   currently 20 s — a stalled stream should fail fast, not hang).
3. **An offline validation gate (REQUIRED before any live call).** A script
   (model on `scripts/probe.py`) that pulls a real TTS MP3 from the socket, runs
   it through the streaming decoder **fed in WS-sized chunks**, and byte-compares
   the resulting PCM against the whole-buffer `decode_mp3_to_pcm16`, plus a
   first-frame latency measurement. The streamed output must match the
   whole-buffer decode (or any delta must be characterized as inaudible). Only
   after this passes does a live call happen. The last regression came from an
   unvalidated audio change going straight to a call.
4. **Risks + fallbacks.** Chunk-boundary glitches, decoder latency under
   event-loop load, the stall/timeout behavior, and keeping barge-in instant
   (epoch flush) and the half-duplex default intact.
5. **An ordered step plan.** Small, independently testable steps, with the
   offline-validation step gated before any live call.

## Process

- Use a planning approach (the Plan agent is a good fit). Read the files above
  before proposing anything. Confirm what is installed (`miniaudio` is a dep;
  `pyav` is not) rather than assuming.
- HARD GATE: deliver the scope + plan and STOP for review. No implementation
  until approved.

## Quick orientation commands

```bash
cd /Users/rschuetz/Code/telnyx-onset
.venv/bin/python -m pytest -q        # 47 tests, no network (should be green)
.venv/bin/ruff check onset/ tests/
.venv/bin/mypy onset/
```
