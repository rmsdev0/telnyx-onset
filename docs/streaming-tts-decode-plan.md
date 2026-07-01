# Plan: streaming / incremental MP3 decode for TTS output

Status: **IMPLEMENTED (Option A) and offline-gate PASSED, behind a default-off
flag.** Steps 1 to 6 are done: ruff clean, mypy --strict clean, 57 tests green,
no network. The offline validation gate (section 4) ran against two live TTS
replies and passed: the streaming decode is byte-identical to the whole-buffer
decode after a 24.0 ms leading-silence offset (miniaudio's whole-buffer decode
emits the MP3 codec priming samples that its streaming decode strips; the leading
region is verified silence, so it is inaudible). Measured first-audio head start
on a real reply: ~5.4 s sooner (0.46 s vs 5.83 s). The only step left is step 7:
flip `tts_streaming_decode=true` and place one live call. The flag ships `false`,
so nothing changes until then.

Note: with the flag on, the streaming path emits ~24 ms less leading silence than
the buffered (default) path. Inaudible, arguably better, but it means the two
modes are not byte-identical at the very start of an utterance.

Branch: `feat/streaming-tts-decode` (off `d44f5a7`, the PR #1 merge = the real
post-PR-1 state; local `main` was never advanced past the first commit).

## 0. Context confirmed (not assumed)

- `.venv/bin/python -m pytest -q` is green: **47 passed**, no network.
- `miniaudio` **1.71** is installed; `pyav` (`import av`) is **not**.
- `miniaudio` exposes the streaming surface the brief names: `stream_any`,
  `stream_memory`, `StreamableSource`, `mp3_stream_file`, `stream_raw_pcm_memory`.
- The seam that matters, from `onset/ports.py`:
  `TtsPort.synthesize(text) -> AsyncIterator[bytes]`. `_speak` (`onset/agent.py`)
  only iterates those frames into `media.send_audio_frame(epoch, frame)`. So the
  entire change is **internal to `onset/tts.py` plus a helper in `onset/audio.py`**.
  Nothing in `_speak`, `MediaStream`, the epoch guard, the mark fence, barge-in,
  or `latency.first_audio` needs to move. `latency.first_audio` already fires on
  the first yielded frame, so it will report the improvement for free.
- TTS socket facts (DISCOVERY.md section 4 and section 13, confirmed live
  2026-06-21): output is **MP3 only**, 16 kHz / 16-bit / mono. Three raw-output
  param guesses all still returned MP3, so the decode is unavoidable. Standalone
  first-audio latency was 0.26 to 0.59 s; in-call the socket streams at ~real time
  and the current `isFinal`-buffering makes time-to-first-audio scale with reply
  length (measured 3.7 to 10.5 s).

## 1. The precise problem and the precise fix

`tts.synthesize()` today accumulates every base64 MP3 chunk until `isFinal`, then
runs one `decode_mp3_to_pcm16` (miniaudio whole-buffer, off-loop via
`asyncio.to_thread`) and yields 640-byte frames. In-call, that means we wait for
the whole reply's MP3 to arrive before the first frame leaves.

Fix: decode the MP3 **incrementally as chunks arrive over the one socket per
turn**, emitting 640-byte L16/16k frames continuously, so the first frame leaves
~one MP3 chunk after synthesis starts (~0.5 to 1 s) and playback keeps pace with
the real-time MP3 stream. The MP3 -> PCM16/16k decode stays the one and only
transcode, still miniaudio, still one vendor.

Dead end (already tried, reverted, do not repeat): per-sentence synthesis with
whole-chunk buffering. The first sentence is still a full ~3 s synth, and each
sentence's full-MP3 synth finishes after the prior sentence's playback ends, so
the pacer starves and you get ~2 s inter-sentence gaps. The win has to come from
decoding *within* the stream, not from chopping the text.

## 2. Decoder options, evaluated

The hard sub-problem in every option is the same: a **sync, pull-based decoder**
has to be driven by an **async WS recv loop**, and the bytes arrive in
WS-sized pieces that do not line up with MP3 frame boundaries (and MP3 has a bit
reservoir, so a frame can depend on bytes from the previous frame).

### Option A. miniaudio `stream_any` + a custom `StreamableSource` (RECOMMENDED)

`miniaudio.stream_any(source, source_format, output_format, nchannels,
sample_rate, frames_to_read)` returns a **sync generator** that pulls encoded
bytes by calling `source.read(num_bytes)` and yields `array.array` chunks of PCM
in the chosen format, resampled to `sample_rate` and downmixed to `nchannels`.
We subclass `miniaudio.StreamableSource` and override `read(num_bytes)` to hand
the decoder MP3 bytes that the WS loop has delivered.

- **Correctness at chunk boundaries:** miniaudio wraps dr_mp3, which owns the bit
  reservoir and frame alignment internally. It consumes exactly the bytes it
  needs and buffers the remainder itself, so feeding it WS-sized pieces is
  transparent. Output is byte-identical to the whole-buffer `decode` by
  construction: same decoder, same bytes, same `sample_rate=16000, nchannels=1`,
  just delivered incrementally. (This is exactly what the section 4 gate proves
  before we trust it.)
- **One critical gotcha:** dr_libs' read callback treats *a short read as
  end-of-stream*. If `read(n)` returns fewer than `n` bytes while more MP3 is
  still coming, the decoder stops early and truncates the audio. Therefore
  `read(n)` MUST block until it can return `n` bytes OR the stream has genuinely
  ended (then it returns what is left, then `b""`). A blocking `read` means the
  generator cannot run on the event loop; it runs in a worker thread. This single
  fact dictates the sync/async bridge below.
- **Bridge (sync decoder <-> async WS):**
  - An async **WS pump task** owns `ws.recv()`. Each MP3 chunk is pushed onto a
    thread-safe `queue.Queue` (`feed_q.put_nowait`). On `isFinal`, error, or the
    per-recv timeout it pushes an EOF sentinel. This task keeps the existing
    `_RECV_TIMEOUT_S` per recv so a missing `isFinal` still cannot hang the turn.
  - The decoder runs in **one worker thread** (`asyncio.to_thread(run_drain)`).
    `StreamableSource.read(n)` does blocking `feed_q.get()` calls, accumulates an
    internal leftover buffer, and returns exactly `n` bytes (or the tail then
    `b""` at EOF). `run_drain` iterates `stream_any(...)`, reframes each PCM chunk
    to 640 bytes through a carryover buffer, and hands frames back to the loop via
    `loop.call_soon_threadsafe(out_q.put_nowait, frame)` (asyncio.Queue is not
    thread-safe; `call_soon_threadsafe` is the supported hand-off). A final
    sentinel on `out_q` signals clean end; a captured exception is re-raised on
    the async side.
  - `synthesize()` starts the pump task and the drain thread, then loops
    `await out_q.get()`, yields each frame, and on the sentinel returns. Its
    `finally` closes the WS, pushes EOF into `feed_q` (so a `read` blocked on an
    empty queue unblocks and the thread exits), and reaps the pump task. This
    preserves today's "close the socket on cancel, abandon the partial synthesis"
    behavior that barge-in relies on.
- **CPU / event-loop impact:** the decode is in a thread, off the loop, same as
  today. The loop-side work (WS recv, queue hand-off, 640-byte reframing) is
  light. The one difference from today is that the executor thread is held for
  the *duration* of synthesis (a few seconds) instead of one short decode call.
  With `max_concurrent_calls=10` that is up to ~10 long-lived executor threads;
  the default ThreadPoolExecutor is `min(32, cpu+4)` workers, so 10 is safe, but
  the plan flags it and section 5 offers a dedicated executor if needed.
- **Dependency weight:** zero new dependencies. miniaudio is already in.
- **Resampling:** `stream_any(sample_rate=16000, nchannels=1)` resamples and
  downmixes in the same single pass as `decode_mp3_to_pcm16` does today. The
  source is already 16k mono, so this is a near no-op and keeps "no resampling in
  app code, one transcode" intact.

### Option B. Repeated whole-buffer decode of the growing buffer (FALLBACK)

Each time a new chunk arrives, re-run `decode_mp3_to_pcm16` on the whole
accumulated buffer and emit only the newly revealed PCM tail (track samples
already emitted, emit the delta).

- **Pro:** reuses the proven `decode_mp3_to_pcm16` verbatim; no thread bridge, no
  `StreamableSource`. Simple to reason about and to keep no-network in tests.
- **Con:** O(n^2) CPU (re-decodes everything each chunk). MP3 is small so this is
  affordable, but the real risk is **correctness at the truncation boundary**:
  decoding a *prefix* of an MP3 can yield a slightly different total sample count
  near the cut (the last frame or two may decode differently until more bytes
  arrive, and encoder delay/padding handling can shift). To stay gap-free and
  glitch-free you must **hold back a safety margin** (do not emit the last ~2
  decoded frames of each partial decode until more data arrives or `isFinal`),
  which adds its own bookkeeping. Net: simpler plumbing, subtler audio
  correctness. Good as a fallback, not as the primary.

### Option C. PyAV / ffmpeg incremental decode (REJECTED)

PyAV gives a clean incremental `CodecContext.parse()/decode()` API and frame-true
output. But it is **not currently a dependency**, pulls in **ffmpeg** (large
wheels, or a system ffmpeg dependency the rest of the stack deliberately avoids,
see `onset/audio.py`: "miniaudio (self-contained wheels, no system ffmpeg)"). It
would introduce a **second, different MP3 decoder** alongside miniaudio, so the
whole-buffer reference path and the streaming path would no longer be the same
codec, undermining the byte-compare gate. The brief says do not add a heavy
dependency or a vendor without justifying it against the alternatives; Option A
covers the requirement with zero new weight, so PyAV is not justified.

### Option D. Hand-rolled MP3 frame parser (REJECTED)

Parse MP3 frame headers, decode whole frames as they complete. This re-implements
exactly the error-prone audio code (bit-reservoir back-references, Xing/Info
header, frame sync) that DISCOVERY.md calls "the most genuinely net-new and
error-prone piece of audio code in this build" and that miniaudio already does
correctly. High bug surface, no upside over Option A. Rejected.

### Recommendation

**Option A** (miniaudio `stream_any` + custom `StreamableSource`, decoder in one
worker thread, two queues bridging to the async WS loop). Zero new dependencies,
same decoder as the reference path (so the byte-compare gate is meaningful),
boundary correctness owned by dr_mp3. Keep **Option B** documented as the fallback
if the thread bridge proves fragile in the gate, and keep a settings flag
(section 5) for instant revert to today's whole-buffer path.

## 3. Recommended design (data flow and where it slots in)

```
WS recv (async pump task)            decoder thread (to_thread)         synthesize() async gen
---------------------------          --------------------------         ----------------------
ws.recv() --base64 MP3-->            stream_any(source=QueueSource,     await out_q.get()
  feed_q.put_nowait(mp3)               source_format=MP3,                 -> yield 640B frame
isFinal/err/timeout:                   output_format=SIGNED16,          -> on sentinel: return
  feed_q.put_nowait(EOF)               nchannels=1, sample_rate=16000)  finally:
                                       |  read(n): block on feed_q,       close ws
                                       |  return exactly n or tail+EOF    feed_q.put(EOF)
                                       v                                  reap pump task
                                     reframe to 640B (carryover)
                                     loop.call_soon_threadsafe(
                                       out_q.put_nowait, frame)
                                     end: put_nowait(SENTINEL)
```

- **Where decode runs:** in one `asyncio.to_thread` worker for the life of the
  turn, off the event loop (same principle as today's single `to_thread` decode).
- **Leftover / partial bytes between chunks:** owned in two places, both
  internal. (1) `QueueSource.read` keeps a leftover bytes buffer so it can return
  exactly `n` bytes across WS-chunk boundaries; dr_mp3 keeps its own
  partial-frame remainder. (2) The reframer keeps a PCM carryover buffer so
  variable-size `stream_any` chunks become exact 640-byte frames; the final
  partial frame is zero-padded by reusing the existing `frame_pcm16` padding rule
  (last syllable not clipped).
- **Resampling:** none in app code; `stream_any` handles it, identical to today.
- **Slotting into `_speak` (all unchanged, by design):**
  - `latency.first_audio` fires on `frames == 0`, i.e. the first yielded frame, so
    it now measures synth + first incremental decode (~0.5 to 1 s) with no edit.
  - The mark fence (`send_mark`) is still sent after all frames are enqueued;
    completion still comes from the echoed mark; the length-scaled `speak.timeout`
    is unchanged.
  - `spoken_tokens` is still appended only after the full `async for` drains, so a
    barge-in mid-synthesis still records nothing.
  - Barge-in cancellation: `_speak`'s task is cancelled -> the `async for` is
    cancelled -> `synthesize()`'s `finally` closes the WS and tears down the
    pump task and decoder thread. Barge-in stays instant because the flush /
    epoch-bump / clear all live in `MediaStream` and fire independently of TTS;
    TTS teardown just stops producing.
  - `_RECV_TIMEOUT_S` (20 s) stays per `ws.recv()` in the pump task; on timeout
    the pump pushes EOF and `synthesize` ends, so a stalled stream fails fast
    instead of hanging, same guarantee as today.

## 4. Offline validation gate (REQUIRED before any live call)

A new script `scripts/validate_streaming_decode.py`, modeled on `scripts/probe.py`
(outbound only, reads `TELNYX_API_KEY` from env, makes a few billable TTS calls,
no public endpoint). It does not touch the loop. It proves the streaming decoder
equals the whole-buffer decoder on a real reply before any call happens. The last
regression came from an unvalidated audio change going straight to a call; this
gate exists to stop that.

Steps the script performs:

1. Connect to the real TTS socket, send a representative utterance, and record
   **each WS message as it arrives**: the decoded MP3 bytes of that chunk and the
   arrival timestamp. This captures real chunk sizes and real timing.
2. Reference path: concatenate all chunks into the full MP3, run the existing
   `decode_mp3_to_pcm16(full_mp3, 16000)` -> `pcm_reference`.
3. Streaming path: feed the **same chunks, in the same WS-sized pieces and order**,
   into the new streaming decoder (`QueueSource` + `stream_any` + reframer) ->
   `pcm_streamed`.
4. **Byte-compare** `pcm_streamed` vs `pcm_reference`. Pass criteria:
   - Exact byte equality is the expected result (same decoder, same bytes).
   - If they differ, characterize the delta and require it to be inaudible:
     report length difference, index of first differing byte, count of differing
     samples, and max absolute sample difference. Acceptable only if the delta is
     confined to trailing zero-padding or a sub-threshold boundary difference, and
     that finding is written down. Anything else fails the gate.
5. **First-frame latency:** measure time from "send text" to the first 640-byte
   frame emitted by the streaming path, and compare to the whole-buffer path
   (which cannot emit until `isFinal`). Report both and the chunk arrival
   timeline, to confirm the head start is real on a real reply.
6. Save both PCM outputs as `.wav` (16k mono) for a human listen, and exit
   non-zero if the byte-compare fails so the gate is scriptable in CI-by-hand.

Only after this script passes does a live call happen.

## 5. Risks and fallbacks

- **Short-read-as-EOF (Option A's main trap):** if `QueueSource.read` ever
  returns fewer than the requested bytes before true EOF, the audio truncates.
  Mitigation: `read` blocks to fill the request; the gate (step 4) catches any
  truncation as a length mismatch before a call.
- **Thread / loop teardown on barge-in:** a decoder thread blocked in
  `feed_q.get()` must always be released. Mitigation: every exit path (cancel,
  error, timeout, normal) pushes EOF into `feed_q`; `call_soon_threadsafe` is
  guarded against a closing loop (`RuntimeError`); `synthesize` does not block the
  loop waiting to join the thread (it abandons it after signaling EOF, the thread
  exits on the next `read`). Unit tests assert socket-closed and no hang on
  mid-stream cancel.
- **Executor thread occupancy:** synthesis now holds one executor thread for a few
  seconds; up to ~10 concurrent calls. Default pool (`min(32, cpu+4)`) covers it.
  Fallback if contention shows up: a dedicated `ThreadPoolExecutor` for TTS decode
  sized to `max_concurrent_calls`, or move the drain to a manually managed
  `threading.Thread`.
- **Chunk-boundary glitches / decoder latency under load:** addressed by Option A
  (dr_mp3 owns boundaries) and by keeping decode off-loop; the gate's byte-compare
  is the proof, and a live listen is the backstop.
- **Stall / timeout:** `_RECV_TIMEOUT_S` stays per recv; stalled stream fails fast.
- **Keeping barge-in instant and half-duplex intact:** untouched. Flush, epoch
  bump, and clear stay in `MediaStream`; the half-duplex gate stays in
  `handle_audio`. TTS teardown only stops the producer.
- **Whole-feature fallback:** a settings flag `tts_streaming_decode: bool`
  selects streaming vs the current whole-buffer path. This gives instant rollback
  with no code change and lets the two paths be A/B compared on one live call.
  (Default value is a review decision: ship `false` and flip after the live call,
  or ship `true` once the gate passes.)

## 6. Ordered step plan (small, independently testable, gated)

1. **`audio.py` helper + reframer (no network).** Add
   `stream_mp3_to_pcm16(source, sample_rate) -> Iterator[bytes]` wrapping
   `miniaudio.stream_any` with explicit `source_format=FileFormat.MP3`, and a
   640-byte reframer with carryover (reusing the `frame_pcm16` padding rule).
   Keep `decode_mp3_to_pcm16` as-is (reference path + tests + gate use it). Unit
   test the reframer and a `StreamableSource` fed from an in-memory buffer in
   small pieces (no network; MP3 correctness is proven by the gate, not unit
   tests, since there is no offline MP3 encoder).
2. **`tts.py` bridge.** Build the WS pump task, the `QueueSource` (blocking
   `read`), the `to_thread` drain, and the new `synthesize()` orchestration that
   yields 640-byte frames. Preserve `_RECV_TIMEOUT_S`, the `{" "}, {text}, {""}`
   framing, provider-error handling, and socket-close-on-cancel.
3. **Tests (`test_tts.py`), still no network.** Keep the existing fake-WS +
   mocked-decode convention. Add: frames emitted incrementally (a frame arrives
   before the `isFinal` message), carryover/partial-frame framing, mid-stream
   cancel closes the socket and stops the thread with no hang, recv-timeout fails
   fast, provider-error path. Mock the decode so tests stay offline.
4. **Validation gate (`scripts/validate_streaming_decode.py`).** Implement section
   4. **GATE: a reviewer runs it with network; byte-compare must pass and the
   first-frame latency win must show.** No live call before this passes.
5. **Settings flag.** Add `tts_streaming_decode` to `settings.py`; branch
   `synthesize` between streaming and legacy whole-buffer. Decide default at
   review.
6. **Quality bar.** `.venv/bin/ruff check onset/ tests/`, `.venv/bin/mypy onset/`
   (strict), full `pytest -q` green. No em dashes. Fully typed.
7. **GATE: one live call** (only after step 4). Confirm `latency.first_audio`
   drops to ~0.5 to 1 s, playback is gap-free, barge-in still instant, half-duplex
   intact.

## 7. Files touched (scope)

- `onset/audio.py`: add streaming helper + reframer (additive; keep existing fns).
- `onset/tts.py`: rewrite `synthesize()` internals (the seam is unchanged).
- `onset/settings.py`: add `tts_streaming_decode` flag.
- `tests/test_tts.py`: extend, still no-network.
- `scripts/validate_streaming_decode.py`: new offline gate (not product code;
  `scripts/` is ruff-excluded per `pyproject.toml`).
- No change to `onset/agent.py`, `onset/media.py`, or `onset/ports.py`.

## 8. Hard constraints check

- Outbound seam unchanged (640-byte L16/16k into `send_audio_frame`): yes.
- Epoch guard, mark fence, barge-in cancel + flush, half-duplex gate: untouched.
- MP3 -> PCM16/16k is the one transcode, still miniaudio, one vendor: yes.
- mypy --strict, ruff, fully typed, Python 3.12: enforced in step 6.
- Tests use fakes, no network: yes (gate is the only network user, run by hand).
- No em dashes: enforced.
- No heavy dependency / no new vendor: yes (zero new deps; PyAV rejected on this).
</content>
</invoke>
