# DISCOVERY: Telnyx Media-Stream Voice Agent (telnyx-onset)

Status: discovery gate. This document resolves the Wiring X vs Y question and the
socket / codec / auth questions with evidence, and proposes the build. Per the
brief, no loop code is written until this is reviewed.

Date: 2026-06-21. Sources: current Telnyx developer docs (fetched live, see
Appendix A), the `team-telnyx/telnyx-hermes-tts` reference plugin, and the two
read-only reference repos `voice-agent-lite` and `telnyx-offleash`.

---

## 0. Headline finding (read this first)

`voice-agent-lite` (at `/Users/rschuetz/Code/voice-agent-lite`) is **already a
working Telnyx media-stream voice agent.** It drives Telnyx Media Streaming in
bidirectional RTP mode, decodes inbound audio, injects outbound audio, flushes
with `{"event":"clear"}` for barge-in, runs a local VAD, and manages the whole
asyncio frame loop. It simply uses **external** Deepgram (STT) and ElevenLabs
(TTS) instead of Telnyx's own STT/TTS services.

Consequence: the "three-socket orchestration" the brief treats as the novel,
risky part is roughly 80 percent already solved and proven in `voice-agent-lite`.
The genuinely new work for `telnyx-onset` is:

1. Replace the Deepgram STT client with a **Telnyx STT WebSocket** client.
2. Replace the ElevenLabs TTS client with a **Telnyx TTS WebSocket** client,
   including an **MP3 decode step** (the Telnyx TTS socket returns MP3, the media
   socket needs raw codec audio).
3. Add the **bounded backpressure and paced injection** the brief requires
   (`voice-agent-lite` currently uses unbounded queues and inline writes).
4. Point the LLM at Telnyx inference (lift `telnyx-offleash`'s `TelnyxLLM`) so the
   entire loop, STT, TTS, LLM, transport, and call control, runs through one
   Telnyx API key.

Everything else (media socket envelope, barge-in state machine, VAD, turn
manager, conversation, limits, retry, logging, the Golden Fork demo) is lifted.

---

## 1. THE GATE: Wiring X vs Wiring Y

**Resolved: Wiring X is real, first-class, and the correct choice for this build.
The application pipes inbound audio to the STT WebSocket itself.**

### Evidence

Telnyx exposes a **standalone Speech-to-Text WebSocket streaming API** that is not
bound to a call:

- Endpoint: `wss://api.telnyx.com/v2/speech-to-text/transcription`
  (Telnyx docs, "Speech-to-Text WebSocket streaming").
- The application **sends binary audio frames** to it (`await ws.send(audio_chunk)`
  in the documented example) and **receives transcript JSON** back
  (`{"transcript": ..., "is_final": ..., "confidence": ...}`).
- No `call_control_id` binding is documented or required. It is a generic
  "audio in, transcript out" socket.

That is exactly Wiring X: two app-managed audio hops (call to app on the media
socket, app to STT on the STT socket). And it is not theoretical: `voice-agent-lite`
already implements this exact shape against Deepgram
(`providers/stt/deepgram_base.py:62-71` feeds mu-law frames it received on the
media socket straight into the STT socket). Swapping the destination socket from
Deepgram's to Telnyx's `wss://api.telnyx.com/v2/speech-to-text/transcription` is a
provider change, not an architecture change.

### Wiring Y also exists, but it is the wrong tool here

Telnyx Call Control has `transcription_start`, which runs server-side transcription
on a call leg and delivers `call.transcription` webhooks. Telnyx also runs Deepgram
Flux on its own GPUs colocated with its telephony PoPs and "feeds [transcripts]
into its call control and agent orchestration layer." That is genuinely Wiring Y
(Telnyx bridges audio to STT server-side). `telnyx-offleash` uses this path.

We deliberately do **not** use Wiring Y for `telnyx-onset`, because:

- Frame-level barge-in needs the app to own the inbound frames anyway (for local
  VAD). Once the app holds the frames, piping them to the STT socket (Wiring X)
  keeps a single coherent audio path and one place to correlate VAD onset with
  transcription. Wiring Y would give two independent audio paths (our media socket
  plus Telnyx's separate server-side transcription) that are harder to align.
- Wiring Y's transcripts arrive as webhooks or a Telnyx-managed stream, which is
  the coarser, higher-latency `telnyx-offleash` model we are explicitly trying to
  beat.

### A third option we are NOT using, for the record

Telnyx **Conversation Relay** streams transcribed text in and accepts text out over
one WebSocket, with Telnyx doing STT, TTS, and turn-taking. That is the easiest
integration but it hands frame ownership (and therefore frame-level barge-in) to
Telnyx. The brief explicitly wants the app to own raw frames, so Conversation Relay
is out of scope. It is the right escape hatch to mention if frame-level control is
ever not worth the complexity.

**Gate conclusion:** Wiring X, via the standalone STT WebSocket, with local VAD as
the barge-in trigger. The app manages all three sockets' audio explicitly. No live
probe was required to settle X vs Y; the docs plus the existence proof in
`voice-agent-lite` are conclusive. (A probe is still recommended for format
details; see Section 9.)

---

## 2. Socket 1: the call media WebSocket (bidirectional RTP)

Inbound to our FastAPI app: Telnyx dials into the `stream_url` we provide. One
bidirectional RTP stream per call.

### Inbound messages Telnyx sends us (verbatim shapes)

```json
{ "event": "connected", "version": "1.0.0" }

{ "event": "start", "sequence_number": "1",
  "start": { "call_control_id": "v2:...", "call_session_id": "...",
             "from": "+1...", "to": "+1...",
             "media_format": { "encoding": "PCMU", "sample_rate": 8000, "channels": 1 } },
  "stream_id": "32DE0DEA-..." }

{ "event": "media", "sequence_number": "4",
  "media": { "track": "inbound", "chunk": "2", "timestamp": "5",
             "payload": "<base64 RTP payload, NO headers>" },
  "stream_id": "32DE0DEA-..." }

{ "event": "stop",  "stop": { "call_control_id": "v2:..." }, "stream_id": "..." }
{ "event": "dtmf",  "dtmf": { "digit": "1" }, "occurred_at": "...", "stream_id": "..." }
{ "event": "error", "payload": { "code": 100004, "title": "invalid_media", "detail": "..." }, "stream_id": "..." }
{ "event": "mark",  "mark": { "name": "..." }, "stream_id": "..." }   // echo of a mark we sent
```

Key facts:
- `media.payload` is "a base64-encoded RTP payload (no headers)." In practice this
  is just the raw codec audio bytes. `voice-agent-lite` confirms **no RTP header
  parsing is needed**: `decode_mulaw_payload` is literally `base64.b64decode`
  (`utils/audio.py:80-82`). We base64-decode and we have the audio.
- Event order is not guaranteed; `media.chunk` (and `dtmf.occurred_at`) can reorder.
- Error codes: 100002 unknown, 100003 malformed_frame, 100004 invalid_media,
  100005 rate_limit_reached.

### Outbound messages we send Telnyx (verbatim shapes)

```json
{ "event": "media", "media": { "payload": "<base64 codec audio>" } }   // inject audio
{ "event": "clear" }                                                    // flush + stop now
{ "event": "mark", "mark": { "name": "response_end" } }                 // playback fence
```

Key facts:
- For Telnyx, outbound `media` and `clear` carry **no `stream_id`** (one stream per
  socket). `voice-agent-lite/transport/telnyx.py:159-166` confirms both shapes.
- Injected RTP chunks may be 20 ms to 30 s. We will pace 20 ms frames.
- **`{"event":"clear"}` "immediately stops the media playing on the stream and
  clears the media queue."** This is the barge-in stop primitive, and it is
  categorically faster than `telnyx-offleash`'s Call Control `playback_stop` (which
  is a REST round trip on the call leg).
- `mark` is echoed back when the immediately preceding media finishes playing.
  This is our "TTS finished playing" signal (replaces `call.speak.ended`).

### Starting the stream

Call Control path (matches `telnyx-offleash`'s control plane). On the inbound call
webhook, answer the call, then start streaming with the documented parameters:

- `stream_url`: our `wss://.../ws/media`
- `stream_track`: `inbound_track` (we only need the caller's audio for STT/VAD;
  outbound is injected by us)
- `stream_bidirectional_mode`: `rtp`
- `stream_bidirectional_codec`: see Section 5

This is available on the Dial command, the Answer command, and the `streaming_start`
action (`POST /calls/{call_control_id}/actions/streaming_start`). Webhooks
`streaming.started` / `streaming.stopped` confirm lifecycle. (Note:
`voice-agent-lite` starts streaming via TeXML `<Stream bidirectionalMode="rtp">`
markup instead; we use the Call Control action for parity with `telnyx-offleash`.
Exact action body to confirm in the probe and against `telnyx-offleash`'s existing
Call Control action code.)

---

## 3. Socket 2: the Telnyx STT WebSocket

- Endpoint: `wss://api.telnyx.com/v2/speech-to-text/transcription`
- Auth: `Authorization: Bearer <TELNYX_API_KEY>` header on the WS handshake.
  **One key, confirmed.**
- Query params: `transcription_engine` (Deepgram, Telnyx, Google, Azure),
  `input_format` (documented examples `mp3`, `wav`; raw PCM / mu-law acceptance is
  the #1 probe item, see Section 9).
- App sends: binary audio frames (`ws.send(bytes)`).
- App receives: `{ "transcript": "...", "is_final": <bool>, "confidence": <float> }`,
  plus an `error` field on failure.
- Independent of any call (no `call_control_id`). This is what enables Wiring X.

Open items (probe): the exact accepted input encoding(s) and sample rate; the
engine/language/end-of-turn config surface; and whether Deepgram Flux's
end-of-turn / interruption / turn signals (the Telnyx Flux materials mention
`eot_threshold` and `eot_timeout_ms`) are exposed on this socket or only on the
Call Control transcription path. Our design does not depend on Flux's signals
because the barge-in trigger is local VAD (Section 6); Flux signals are an optional
enhancement to turn-taking if exposed.

---

## 4. Socket 3: the Telnyx TTS WebSocket

- Endpoint: `wss://api.telnyx.com/v2/text-to-speech/speech?voice=<voice>`
  (e.g. `voice=Telnyx.NaturalHD.astra`). Optional `inactivity_timeout`.
- Auth: `Authorization: Bearer <TELNYX_API_KEY>` header on the WS handshake.
  **One key, confirmed.**
- App sends (JSON): `{"text": " "}` to init, then one or more `{"text": "..."}`
  with content, then `{"text": ""}` to end synthesis.
- App receives (JSON): `{"audio": "<base64 MP3 chunk>", "isFinal": <bool>}`.
  Output is **MP3, 16 kHz, 16-bit, mono. MP3 is the only documented output.**
- Voices: `Telnyx.NaturalHD.*` and `Telnyx.KokoroTTS.*` are first-party Telnyx
  engines; AWS Polly, Azure, and ElevenLabs voices are also selectable via the
  `voice` param (exact ids from the live catalog).

### The one unavoidable transcode

The TTS socket emits **MP3**, but the media injection path wants **raw codec audio**
(PCMU / L16 / etc). So the TTS bridge must **decode MP3 to PCM** before framing and
injecting. This is the single transcoding step in the whole system. Candidate
decoder: `miniaudio` (self-contained wheels, no system ffmpeg, decodes MP3 to PCM16
and can resample). Final choice depends on the probe (if an undocumented raw/PCM
output param exists on the TTS socket, the decode disappears).

`voice-agent-lite` did **not** need this because ElevenLabs emits mu-law directly
(`output_format="ulaw_8000"`). So the TTS bridge is the most genuinely net-new and
error-prone piece of audio code in this build. It gets the rigor.

Open items (probe): any raw/PCM/mu-law output option; the exact `isFinal` framing;
a mid-synthesis cancel mechanism (none documented, plan in Section 6); non-Telnyx
voice ids.

---

## 5. Codec and sample rate, end to end

The brief asks for one consistent codec to avoid resampling, or documented
resampling. The constraint that drives the decision: the TTS socket outputs MP3
16 kHz, and STT engines natively prefer 16 kHz PCM, but the telephone call leg is
8 kHz G.711.

### Recommended: L16 / 16 kHz as the internal canonical rate

- Media socket: `stream_bidirectional_codec = L16` (16 kHz, linear PCM16).
- Inbound: base64-decode to PCM16 16 kHz. No mu-law decode (L16 is already linear),
  feed straight to WebRTC VAD (native 16 kHz) and to STT.
- STT: send PCM16 16 kHz, the engines' preferred input.
- TTS: decode MP3 16 kHz to PCM16 16 kHz, frame to 20 ms (640 bytes), inject.

Result: a single internal rate (PCM16 / 16 kHz), **zero resampling and zero mu-law
math in app code**, and exactly one transcode in the system (MP3 decode on TTS).
Telnyx transcodes between the 8 kHz PSTN leg and our 16 kHz stream transparently at
the carrier. For a phone demo the PSTN audio is band-limited regardless, so this
costs nothing perceptible and keeps the new code minimal (new code is where bugs
live).

### Alternative: PCMU / 8 kHz (maximize voice-agent-lite reuse)

- Media socket: `PCMU` (mu-law 8 kHz), exactly `voice-agent-lite`'s default. The
  entire inbound + VAD + media path lifts byte-for-byte and is already proven, with
  no carrier transcoding.
- Cost: the TTS bridge must decode MP3, **downsample 16 kHz to 8 kHz**, and
  **encode PCM16 to mu-law** (`voice-agent-lite` has the mu-law decoder and the
  8k-to-16k upsampler but **not** a PCM16-to-mu-law encoder or a downsampler, so
  both are net-new). More audio code on the most error-prone path.

**Recommendation: L16 / 16 kHz**, decided finally after the probe confirms STT
accepts L16 16 kHz input and TTS has no raw output option. If the probe shows STT
will not cleanly take L16 or TTS can emit mu-law 8 kHz directly, fall back to
PCMU 8 kHz. This is the main open decision for review (Section 12).

---

## 6. Barge-in design

### Trigger source: local WebRTC VAD on inbound frames (primary)

Decision: **local WebRTC VAD (`webrtcvad-wheels`, stateless, no ONNX) is the
barge-in trigger.** STT finals drive turn content and endpointing. Deepgram Flux's
interruption signal, if exposed on the STT socket, is an optional secondary confirm.

Justification:
- Lowest latency: VAD fires on acoustic onset (about 120 ms of contiguous speech in
  `voice-agent-lite`'s default) with no network round trip. STT-signal barge-in
  waits for the STT socket to detect and return, which is slower.
- App owns the frames already (it must, for Wiring X), so VAD adds no new data path.
- No dependency on whether the standalone STT socket exposes Flux turn signals
  (unconfirmed; see Section 3).
- The brief explicitly wants the WebRTC engine, stateless, no ONNX.
  `voice-agent-lite` ships both `webrtcvad` and Silero/ONNX; we take WebRTC and
  drop Silero/ONNX.

This is also precisely why `telnyx-onset` will interrupt perceptibly faster than
`telnyx-offleash`: offleash detects speech server-side via STT, then issues a
`playback_stop` REST call (two network legs); onset detects locally on frames it
already holds and flushes in one outbound WS frame.

### Stop sequence (the frame-level primitive)

On VAD onset while the agent is speaking:
1. Send `{"event":"clear"}` on the media socket (flush Telnyx's outbound queue,
   stop playback immediately).
2. Stop the local injection pacer and drop any buffered outbound frames.
3. Cancel the in-flight response task (LLM plus TTS), bumping a generation counter
   so late TTS audio for the dead turn is discarded.
4. Roll conversation history back to what was actually played (`spoken_tokens`),
   tagged `[interrupted]`; drop the turn entirely if nothing was spoken.
5. `turn_manager.set_listening()` and return to listening.

Steps 1, 3, 4, 5 already exist in `voice-agent-lite/barge_in.py` plus the agent
dispatch (`agent.py:236-244`) and in `telnyx-offleash/barge_in.py`. The barge-in
state machine takes the stop action as an injected coroutine, so we inject the
media-socket flush and the state machine is unchanged. Step 2 (stop the pacer) is
net-new and pairs with the paced-injection writer (Section 7).

### TTS cancellation detail

No mid-synthesis cancel is documented on the TTS socket. Plan: keep one warm TTS
socket per call to avoid per-turn reconnect latency, and on barge-in (a) stop
forwarding TTS audio into the pacer, (b) `clear` the media queue, (c) discard
remaining audio for the cancelled generation via the generation guard. If keeping
the socket warm proves to leak audio across turns, fall back to a per-turn TTS
socket that we close on turn end or barge-in (simpler cancellation, higher
first-audio latency). Resolve during build with the probe's latency numbers.

---

## 7. Concurrency model (sockets, owners, teardown, backpressure)

Runtime is `asyncio` (stdlib) throughout, matching both reference repos. Per call:

| Task | Owns | Lifecycle |
| --- | --- | --- |
| Media WS read loop (FastAPI handler) | the inbound media socket (sole reader) | ends on `stop` / socket close; `finally` cancels the agent task and releases the capacity slot |
| `agent.run()` | the orchestration loop | ends when STT stream ends or on hangup; `finally` closes STT, TTS, VAD |
| STT recv task | the STT socket | created on connect, cancel-and-gather on teardown |
| TTS recv task | the TTS socket | created on connect, cancel-and-gather on teardown |
| Per-turn response task | one LLM + TTS turn | the barge-in cancellation target; only one live at a time |
| Injection pacer (new) | the outbound 20 ms cadence | started per response, stopped on turn end or barge-in |
| VAD inference | offloaded via `asyncio.to_thread` per frame | so per-frame CPU never stalls audio |

Teardown discipline (lifted from `voice-agent-lite`): capture-and-clear task refs,
`task.cancel()` then `await asyncio.gather(..., return_exceptions=True)` at each
layer; STT/TTS close sends a close frame then a queue sentinel to end the event
iterator. Every socket has one owner and one cancellation path, torn down on
hangup, on `stop`, on socket error, and on agent teardown. This satisfies the
"no orphaned socket tasks" bar.

### Backpressure: the gap we must close

`voice-agent-lite` uses **unbounded** `asyncio.Queue`s and drop-on-failure on the
STT feed. The brief requires bounded or drop-oldest on the detection/transcription
path and correct pacing on the injection path. Net-new work:

- Bounded (drop-oldest) queues on the STT-feed and VAD-feed paths, so a slow STT
  socket cannot grow memory without bound; dropping oldest audio degrades
  transcription gracefully rather than bloating latency.
- A paced injection writer: decode TTS MP3 to PCM, slice to 20 ms frames, and inject
  on a 20 ms cadence keeping only a shallow lead buffer in Telnyx. Shallow lead plus
  `clear` is what makes barge-in crisp; flooding (dumping all frames at once) would
  leave a large queue in Telnyx that `clear` must flush, and starving would cause
  audible gaps.

### Graceful degradation

Any one of the three sockets dropping mid-call must tear down cleanly, not hang the
call. STT drop: attempt bounded reconnect (the `voice-agent-lite` pattern), and if
it fails, end the turn and hang up cleanly. TTS drop: abort the current response,
optionally speak a short fallback via a fresh socket, else hang up. Media drop: the
call is gone, tear everything down.

---

## 8. Auth and minimal setup surface

**One Telnyx API key (Bearer) authenticates the entire loop:** Call Control REST
(answer, `streaming_start`, hangup, dial), the STT WS handshake, the TTS WS
handshake, and LLM inference (`https://api.telnyx.com/v2/ai`, OpenAI-compatible,
used by `telnyx-offleash`'s `TelnyxLLM`). This is the honest single-vendor story:
STT, TTS, LLM, voice transport, and call control are all `api.telnyx.com` behind
one key.

The inbound media socket is the one reversed direction (Telnyx connects to us). We
secure it with Ed25519 webhook signature verification (`TELNYX_PUBLIC_KEY`, via
`pynacl`) on the call-control webhooks that set up the call, plus an unguessable
`stream_url`. Both reference repos already implement the signature verification.

Minimal Telnyx configuration:
- A Voice API (Call Control) application with a phone number assigned.
- Webhook URL pointing at our `/webhook` (call lifecycle events).
- `stream_url` pointing at our public `wss://.../ws/media`.

Call flow: inbound call -> `call.initiated` webhook -> we answer and
`streaming_start` (bidirectional rtp, chosen codec) -> Telnyx dials our media WS ->
we open the STT and TTS sockets (Bearer) -> loop. Hangup -> `stop` event plus
webhook -> tear down all three sockets.

Credentials are already present in both sibling repos' `.env`
(`TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_CONNECTION_ID`, a phone number), so
a live probe and a live call test are possible immediately on approval.

---

## 9. Residual unknowns and the recommended live probe

RESOLVED LIVE: every item in this section was confirmed against the Telnyx API by
`scripts/probe.py`. See Section 13 for the confirmed params, schemas, and the exact
working STT config. This section is retained for the original reasoning.

The Wiring X/Y gate is resolved from docs. The remaining unknowns are audio-format
details that change implementation, not architecture. They are best nailed with a
small live probe as **build step 0**, before wiring the loop:

1. STT WS: exact accepted input encoding(s) and sample rate (does it take raw L16
   16 kHz or mu-law 8 kHz binary frames, or only `mp3`/`wav` containers?), and the
   engine/language/end-of-turn config surface.
2. STT WS: whether Flux end-of-turn / interruption / turn events are exposed beyond
   `{transcript, is_final, confidence}`, and whether `is_final` endpointing alone is
   adequate for turn-taking (else rely on VAD `UtteranceEnd`).
3. TTS WS: any raw/PCM/mu-law output option (would remove the MP3 decode), the exact
   `isFinal` framing, the mid-synthesis cancel mechanism, and non-Telnyx voice ids.
4. The exact `streaming_start` Call Control action body for bidirectional RTP.
5. First-audio latency of the STT and TTS sockets (informs warm vs per-turn TTS
   socket, and sets the barge-in latency baseline to beat).

Proposed probe: `scripts/probe.py`, roughly 80 to 120 lines, live, reads
`TELNYX_API_KEY` from env. It (a) opens the TTS socket, synthesizes "hello", dumps
the first audio message keys and the decoded byte header to confirm MP3 vs raw and
measures first-audio latency; (b) opens the STT socket, feeds a known speech sample
in a few candidate encodings, and dumps every message to confirm accepted input and
transcript/turn shapes; (c) optionally places one real call via Call Control to
confirm bidirectional RTP media plus `clear`. It writes findings back into this
document. This is the only step that makes live (billable) Telnyx calls.

---

## 10. Positioning and honesty

Identity: one control plane, one Telnyx API key, one vendor relationship. This is
literally true here: transport, call control, STT, TTS, and LLM inference are all
`api.telnyx.com` behind one Bearer key.

Model attribution rules we will follow in code, names, and docs:
- TTS default `Telnyx.NaturalHD.*` (and `Telnyx.KokoroTTS.*`) are genuinely
  first-party Telnyx engines, so calling the voice "a Telnyx voice" is accurate when
  one of those is selected.
- STT via Deepgram Flux (recommended for turn-taking quality) is a **third-party**
  model reached through Telnyx's API. We will label it "Deepgram Flux via Telnyx's
  single API," never "Telnyx STT." If an all-first-party story is preferred, select
  the `Telnyx` STT engine instead and accept whatever turn-taking it provides.
- We never claim a model is first-party Telnyx unless a Telnyx-built engine is
  actually selected. The single-vendor claim is about the control plane and API
  surface, not model authorship.

---

## 11. Reuse inventory (lift map)

Both reference repos share lineage (`telnyx-offleash` was forked from
`voice-agent-lite`'s media-stream architecture and collapsed onto Call Control), so
many modules exist in both; we take the cleaner of each. The Golden Fork demo is
present in both.

### From voice-agent-lite (the media-stream spine)

| Module | Verdict | Note |
| --- | --- | --- |
| `utils/audio.py` | LIFT, then EXTEND | base64 + mu-law + 8k->16k. Add MP3 decode wrapper; add PCM16->mu-law + downsample only if PCMU path chosen |
| `transport/base.py` | LIFT-AS-IS | `Transport` / `OutboundChannel` protocol, the seam the sockets plug into |
| `transport/telnyx.py` | LIFT, REWIRE | inbound `decode` + outbound `media`/`clear`/`mark` shapes; rewire stream start to Call Control |
| `barge_in.py` | LIFT-AS-IS | state machine takes the stop action as an injected coroutine |
| `turn_manager.py` | LIFT-AS-IS | STT-event to turn accumulation |
| `turn_detection/` (WebRTC engine) | LIFT-AS-IS | drop Silero/ONNX per brief |
| `providers/base.py`, `providers/stt/base.py`, `providers/stt/websocket_base.py` | LIFT-AS-IS | subclass the WS STT base for Telnyx STT |
| `agent.py` | LIFT, REWIRE | provider construction -> Telnyx sockets; add paced injection + bounded queues |
| `main.py`, `config.py` | LIFT, REWIRE | three-socket startup in the `Start` case; strip Twilio |

### From telnyx-offleash (agent core, demo, LLM)

| Module | Verdict | Note |
| --- | --- | --- |
| `prompts.py` | LIFT-AS-IS | Golden Fork / Ava / 3-node flow, parity-critical |
| `tools.py` | LIFT-AS-IS | 3 tools, keep the `zlib.crc32` deterministic confirmation number |
| `telnyx.py` (the `TelnyxLLM` half) | LIFT-AS-IS | AsyncOpenAI -> Telnyx inference, `moonshotai/Kimi-K2.5`, stream, `enable_thinking=False`, `max_tokens` only on tool-free turns. Pin the model deliberately (Telnyx is moving to K2.6) |
| `limits.py` (`TokenBudget`, `CallLimiter`) | LIFT-AS-IS | per-call token budget + concurrency cap |
| `retry.py` | LIFT-AS-IS | retry-before-first-yield, correct for streaming TTS too |
| `logging.py`, `types.py` | LIFT-AS-IS | structlog config; event vocabulary (extend types with an audio-frame type) |
| `tests/conftest.py` fakes (`FakeLLM`, `text_round`, `tool_round`, `until`) | LIFT-AS-IS | LLM-contract-level, no network |

### Net-new (the actual build)

- `stt.py`: Telnyx STT WebSocket client (subclass the WS STT base).
- `tts.py`: Telnyx TTS WebSocket client + MP3 decode + sentence buffering.
- Paced injection writer + bounded (drop-oldest) backpressure queues.
- `scripts/probe.py`: the live format probe (build step 0).
- Fake STT/TTS socket pairs for the new surface (model on `voice-agent-lite`'s
  `FakeWebSocket` / `FakeSTT` / `FakeTTS`).

---

## 12. Open decisions for review

1. **Codec:** RESOLVED. L16 / 16 kHz, live-validated end to end (Section 13).
2. **STT engine:** Deepgram chosen. CAVEAT from the probe: Flux's native turn
   protocol is not exposed on the standalone WS; `transcription_engine=Deepgram`
   returns a normalized `{transcript, is_final, speech_final}` schema, and the
   Telnyx first-party engine is the same schema one query-param away. So the choice
   is now accuracy (Deepgram) vs all-first-party framing (Telnyx), not turn-taking.
   See Section 13. Flag for confirmation.
3. **Run the live probe now?** DONE. Findings in Section 13.
4. **Agent core source:** graft `telnyx-offleash`'s cleaner core (TokenBudget,
   TelnyxLLM, Golden Fork) onto `voice-agent-lite`'s media spine. Confirm.
5. **Stream start mechanism:** Call Control `streaming_start` action (recommended,
   matches offleash) vs TeXML `<Stream>` (voice-agent-lite's approach).

---

## 13. Probe findings (confirmed live against the Telnyx API, 2026-06-21)

`scripts/probe.py` connected to the live STT and TTS sockets with the production
API key. All residual unknowns from Section 9 are resolved.

### Auth (confirmed)
One `Authorization: Bearer <TELNYX_API_KEY>` handshake header authenticates both the
STT and TTS WebSockets (no 401s). With offleash's Call Control and LLM usage of the
same key, one key covers the entire loop.

### TTS WebSocket (confirmed)
- `wss://api.telnyx.com/v2/text-to-speech/speech?voice=Telnyx.NaturalHD.astra`.
- Send `{"text":" "}`, then `{"text":"<content>"}`, then `{"text":""}`.
- Receive `{"audio":"<base64 MP3>","isFinal":<bool>,"text":...,"cached":<bool>}`.
- Output is MP3 only. Three raw-output param guesses (`output_format=pcm`,
  `encoding=pcm_s16le`, `output_format=l16`) all still returned MP3, so the MP3
  decode is unavoidable. `miniaudio` decodes MP3 to PCM16/16k/mono cleanly.
- First-audio latency 0.26 to 0.59s (cached repeats ~0.3s; `cached:true` on repeat).

### STT WebSocket (confirmed): the real-loop path
- `wss://api.telnyx.com/v2/speech-to-text/transcription`.
- Verified working config for the L16/16k loop, streaming raw PCM frames at 20 ms:
  `?transcription_engine=Deepgram&input_format=linear16&sample_rate=16000&language=en&interim_results=true`
  Send raw PCM16/16k binary frames; transcripts stream incrementally (interims
  during speech, then a final). No transcode from the media socket.
- The selector param is `input_format`, NOT `encoding`. `encoding=linear16` is
  ignored (0 messages). Supported `input_format` values (from a 40001 error):
  `mp3, wav, webm, ogg, flac, ogg_opus, webm_opus, linear16, linear32` (and `mulaw`
  also transcribes). So raw L16 streaming = `input_format=linear16`.
- Transcript schema: `{"transcript":str,"confidence":float,"is_final":bool,"speech_final":bool}`.
  Turn mapping: interim (is_final false) to TRANSCRIPT_INTERIM; is_final true to
  TRANSCRIPT_FINAL; speech_final true to UTTERANCE_END (turn complete). On
  endpointing a trailing `{"transcript":"","is_final":true,"speech_final":true}` arrives.
- Supported `transcription_engine` values (from a 40007 error): `AssemblyAI, Azure,
  Deepgram, Google, Soniox, Speechmatics, Telnyx, xAI`. Both Deepgram (third-party
  via Telnyx) and Telnyx (first-party) are one query-param apart.

### Deepgram Flux: not exposed as a turn protocol on this socket (important)
- `transcription_engine=deepgram/flux` is invalid (only `Deepgram` is a valid engine).
- `model=flux` / `model=flux-general-en` did not engage Flux (0 messages or default).
- `transcription_model=flux-general-en` is accepted, but output stays the normalized
  `{transcript,is_final,speech_final}` schema, NOT Flux's native turn protocol
  (StartOfTurn / EndOfTurn / EagerEndOfTurn).
- Conclusion: the standalone STT WS serves a nova-style normalized transcript
  stream. Flux's end-of-turn / eager-end-of-turn signals (the speculative
  turn-taking enablers) are not surfaced here; they would require the Call Control
  transcription path (Wiring Y) or Telnyx support.
- Impact on the core loop: none. Barge-in is local VAD (independent of Flux);
  turn-taking uses `speech_final`, which is present and sufficient. We run
  `transcription_engine=Deepgram` and consume the normalized schema.

### Codec decision validated
L16 / 16 kHz is the clean single-rate path, confirmed end to end: media socket L16
16k, inbound PCM16 fed directly to STT `input_format=linear16&sample_rate=16000` (no
transcode), TTS MP3 16k decoded to PCM16 16k and framed to 20 ms for injection (the
single transcode). No resampling and no mu-law in app code.

---

## Appendix A: sources consulted

- Telnyx, "Media Streaming over WebSockets" (message shapes, bidirectional RTP,
  `clear`, `mark`, codecs, `streaming_start`).
- Telnyx, "Speech-to-Text WebSocket streaming"
  (`wss://api.telnyx.com/v2/speech-to-text/transcription`, Bearer, binary frames,
  `{transcript, is_final, confidence}`).
- Telnyx, "Text-to-Speech WebSocket Streaming"
  (`wss://api.telnyx.com/v2/text-to-speech/speech?voice=...`, Bearer, `{text}` in,
  `{audio, isFinal}` MP3 out).
- `team-telnyx/telnyx-hermes-tts` (reference TTS WS protocol, NaturalHD / Kokoro).
- Telnyx, "Run Deepgram Flux on Telnyx" and STT launch notes (Flux as primary
  engine, `eot_threshold` / `eot_timeout_ms`, colocated GPUs = Wiring Y exists).
- Reference repos (read-only): `voice-agent-lite` (media-stream spine, line refs
  throughout this doc) and `telnyx-offleash` (agent core, `TelnyxLLM`, Golden Fork,
  and its own `DISCOVERY.md` documenting the original media-stream seams).
