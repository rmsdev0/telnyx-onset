# telnyx-onset

A real-time voice agent on the Telnyx **media-stream** architecture: raw
bidirectional audio over the Telnyx Media Streaming WebSocket, real-time STT and
TTS over Telnyx WebSocket services, and **frame-level barge-in**. The entire loop
(voice transport, STT, TTS, LLM inference, and call control) runs through one
Telnyx control plane and one API key.

Status: validated end to end on a real inbound call in the default half-duplex
(turn-based) config; frame-level barge-in ships opt-in behind an echo gate (see
below).

Identity: one control plane, one API key, one vendor relationship. Not "every
model is Telnyx-built." The TTS voice (`Telnyx.NaturalHD.*`) is a first-party
Telnyx engine, while STT runs Deepgram via Telnyx's single API (a third-party
model on one control plane, never relabeled "Telnyx STT"). See
[DISCOVERY.md](DISCOVERY.md) for the architecture, the live protocol findings,
and the design decisions.

This is the sibling of `telnyx-offleash` (the Call Control / webhook path). The
agent core (turn manager, conversation, tool loop with flow nodes, token budget,
retry, the LLM client, and the Golden Fork demo) is lifted from that work; the
new code here is the three-socket media-stream wiring and frame-level barge-in.

## The loop (three sockets, one key)

1. **Media socket** (`media.py`): Telnyx dials our `/ws/media`; one bidirectional
   RTP stream per call. Inbound L16 frames in, paced L16 frames out, a `clear`
   event to flush instantly. The barge-in primitive.
2. **STT socket** (`stt.py`): we stream the caller's L16 frames to
   `wss://api.telnyx.com/v2/speech-to-text/transcription` (Deepgram,
   `input_format=linear16`) and read `{transcript, is_final, speech_final}`.
3. **TTS socket** (`tts.py`): we send text to
   `wss://api.telnyx.com/v2/text-to-speech/speech`, decode the MP3 it returns to
   PCM16, and pace it into the call.

Barge-in: a local WebRTC VAD (`vad.py`) detects speech onset on the inbound
frames and, while the agent is speaking, flushes the outbound queue with `clear`
and cancels the in-flight LLM and TTS. No network round trip, so it interrupts
perceptibly faster than the Call Control `playback_stop` path.

The default config is **half-duplex** (`HALF_DUPLEX=true`): the agent stops
listening while it speaks so it cannot interrupt itself through line or acoustic
echo, which gives clean turn-taking on a real phone. Set `HALF_DUPLEX=false` to
enable the frame-level barge-in above once the audio path is confirmed echo-free.

Audio is L16 / 16 kHz end to end, so nothing is resampled in app code; the only
transcode in the loop is decoding the TTS socket's MP3.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env   # then fill in TELNYX_API_KEY, TELNYX_PUBLIC_KEY, etc.
```

On Telnyx: a Voice API (Call Control) application with a number assigned, its
webhook URL pointing at this server's `/webhook`, and `MEDIA_STREAM_URL` set to
the public `wss://` address of `/ws/media` (an ngrok or cloudflared tunnel in
development).

## Run

```bash
.venv/bin/python -m onset
```

The server answers inbound calls, issues `streaming_start`, and runs the loop
when Telnyx connects the media socket.

## Verified on a real call

The loop has been validated end to end on a real inbound PSTN call: the agent
greets, the caller is transcribed via the STT socket, the LLM (Telnyx inference)
responds, the Golden Fork reservation flow runs to completion (check
availability, take the name, book, read back a confirmation number), TTS audio
plays back to the caller through frame injection, relative dates resolve from the
current date in the prompt, and the call tears down cleanly on hangup with the
STT, media, and LLM sockets all closed and no orphaned tasks. This was in the
default half-duplex config, so the conversation is turn-based; frame-level
talk-over interruption is the opt-in full-duplex mode above and has not been
validated on a live call.

The automated suite and the live socket probe (`scripts/probe.py`, which
confirmed the STT and TTS protocols against the real Telnyx API; see
[DISCOVERY.md](DISCOVERY.md) Section 13) run with no phone. `scripts/probe.py` is
a one-off diagnostic, not part of the agent.

To reproduce on your own number:

1. Start a tunnel to the local port (e.g. `ngrok http 8000`) and set
   `MEDIA_STREAM_URL=wss://<tunnel-host>/ws/media` and the Telnyx webhook to
   `https://<tunnel-host>/webhook`.
2. Run the server and call the Telnyx number.
3. You hear the greeting, book a table (give a date, time, party size, and name),
   and the agent reads back a confirmation number, then hangs up cleanly.

## Quality

```bash
.venv/bin/python -m pytest          # tests, no network
.venv/bin/ruff check onset/ tests/
.venv/bin/mypy onset/ tests/        # --strict via pyproject
```

## Out of scope (this session)

A polished CLI, one-click deploy, packaging, and multi-call tuning beyond "does
not break with a few concurrent calls" are deliberately deferred. The entrypoint
is intentionally minimal.
