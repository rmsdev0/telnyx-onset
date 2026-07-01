"""Flat, env-driven settings.

One Telnyx API key authenticates every layer of the media-stream loop: Call
Control (answer, streaming_start, hangup), the STT WebSocket, the TTS WebSocket,
and the LLM inference endpoint. No provider selection and no config framework,
just the values this single-control-plane agent needs.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telnyx auth: one key covers Call Control, the STT and TTS WebSockets, and
    # LLM inference.
    telnyx_api_key: str
    telnyx_public_key: str

    # Telephony identity
    telnyx_phone_number: str = ""  # E.164, the "from" number for outbound
    telnyx_connection_id: str = ""  # Voice API Application id (connection_id)

    # Telnyx API surfaces
    telnyx_api_base: str = "https://api.telnyx.com/v2"
    telnyx_ws_base: str = "wss://api.telnyx.com/v2"

    # Public wss:// address Telnyx dials for the bidirectional media stream. Must
    # be this server's /ws/media route, reachable from Telnyx (e.g. a tunnel).
    media_stream_url: str = ""

    # The media socket, the STT socket, and decoded TTS all run at one rate so
    # nothing is resampled in app code: linear PCM16 at 16 kHz. The only
    # transcode in the whole loop is decoding the TTS socket's MP3 output.
    media_codec: str = "L16"
    sample_rate: int = 16000
    frame_ms: int = 20

    # Where injected audio is played. On a single answered inbound call (no
    # bridge) "opposite" targets a non-existent leg, so the caller hears nothing;
    # "self" injects onto the caller's own leg so they hear the agent.
    stream_bidirectional_target_legs: str = "self"

    # LLM inference (OpenAI-compatible Telnyx endpoint). enable_thinking is
    # disabled for real-time latency in the LLM wrapper.
    llm_base_url: str = "https://api.telnyx.com/v2/ai"
    llm_model: str = "moonshotai/Kimi-K2.5"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 200

    # STT WebSocket. Deepgram is a third-party model reached through Telnyx's
    # single API (never relabel it "Telnyx STT"); set stt_engine="Telnyx" for an
    # all-first-party engine, same schema and wiring. input_format=linear16
    # takes the media socket's L16 audio with no transcode. speech_final drives
    # turn-taking; barge-in is the local VAD.
    stt_engine: str = "Deepgram"
    stt_input_format: str = "linear16"
    stt_language: str = "en"
    stt_max_reconnects: int = 2
    stt_reconnect_backoff_s: float = 0.5
    # Inbound-to-STT queue bound (frames); drop-oldest when full.
    stt_feed_max_frames: int = 50

    # TTS WebSocket. NaturalHD is a first-party Telnyx voice tier. Output is MP3,
    # decoded to PCM16 at sample_rate for injection.
    tts_voice: str = "Telnyx.NaturalHD.astra"
    # Decode the TTS MP3 incrementally as it streams off the socket (emit frames
    # as chunks arrive) instead of buffering the whole reply before decoding.
    # Cuts time-to-first-audio on long replies (offline gate validated the
    # streamed decode matches the whole-buffer decode). Set false to fall back to
    # the whole-buffer path.
    tts_streaming_decode: bool = True
    # Milliseconds of decoded audio to buffer before playback starts (streaming
    # decode only). Telnyx sends the first MP3 chunk fast, then pauses ~450 ms
    # before the rest streams; without a cushion the pacer starves for a few
    # frames right after a reply begins. This absorbs that early gap into the
    # first-audio latency instead of an audible stutter. 0 disables prebuffering.
    tts_prebuffer_ms: int = 400

    # Barge-in VAD (WebRTC, stateless, no ONNX). aggressiveness 0-3.
    vad_aggressiveness: int = 2
    vad_speech_onset_ms: int = 120
    vad_silence_rearm_ms: int = 500

    # Outbound injection: the bounded lead (frames) Telnyx may buffer ahead of
    # the pacer. A shallow lead plus clear is what makes barge-in crisp.
    inject_lead_frames: int = 25

    # Half-duplex listening. While the agent is speaking (and for a short guard
    # after), drop inbound audio so it never transcribes or barges in on its own
    # voice through line or acoustic echo. This makes the turn-based loop work on
    # a real phone. Set false for full-duplex frame-level barge-in once the audio
    # path is confirmed echo-free (otherwise the agent interrupts itself).
    half_duplex: bool = True
    listen_guard_ms: int = 300

    # Spend and volume protection (0 disables)
    max_concurrent_calls: int = 10
    max_tokens_per_call: int = 0

    # LLM stream retry
    stream_max_retries: int = 2
    stream_retry_backoff_s: float = 0.25

    # Webhook signature freshness window (seconds)
    webhook_tolerance_s: int = 300

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    env: str = "development"
    log_level: str = "info"

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def frame_bytes(self) -> int:
        """Bytes in one frame of mono PCM16 at sample_rate (640 at 16 kHz/20 ms)."""
        return int(self.sample_rate * self.frame_ms / 1000) * 2


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process from the environment and .env file."""
    return Settings()  # type: ignore[call-arg]
