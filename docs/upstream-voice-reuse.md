# Existing Hermes Voice Reuse

The implementation seams below were verified against Hermes in August 2026. Re-check current upstream documentation before changing a seam.

## Official documentation

- Voice Mode: https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode
- Wake Word: https://hermes-agent.nousresearch.com/docs/user-guide/features/wake-word
- Desktop: https://hermes-agent.nousresearch.com/docs/user-guide/desktop
- Voice & TTS: https://hermes-agent.nousresearch.com/docs/user-guide/features/tts
- API Server: https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server

## Existing behavior used by TalkToHermes

| Need | Existing Hermes seam | TalkToHermes action |
|---|---|---|
| Agent identity and tools | official sessions/runs API | call, do not wrap another LLM |
| Persistent conversation | `/api/sessions`, session messages, `/v1/runs` | store only mapping; forward canonical Hermes history |
| Streaming status | run SSE events | translate/redact for mobile |
| Approvals and stop | run approval/stop endpoints | expose only once/deny |
| Spoken-text cleanup | `tools.tts_text_normalize.prepare_spoken_text` | call through worker |
| Long-form TTS | `tools.tts_tool.text_to_speech_tool` | call with provider override |
| OpenAI-compatible TTS seam | `tts.openai.base_url` | reusable with the dedicated `/audio/speech` adapter; a legacy `/synthesize` wrapper is incompatible |
| Piper | built-in `piper` provider plus Wyoming protocol | configured remote voice, then configured local voice |
| Local STT recovery | `transcribe_audio_local_fallback` | call through worker |
| Wyoming STT | installed named command adapter | call the configured packaged adapter |
| GPU STT | dedicated `/v1/audio/transcriptions` | dedicated bounded HTTPS adapter |
| Desktop sentence behavior | `SentenceChunker` and `/api/audio/speak-stream` | contract reference, no copy |

## Deliberately not exposed

Hermes Desktop's authenticated endpoints already implement audio:

```text
POST /api/audio/transcribe
POST /api/audio/speak
WS   /api/audio/speak-stream
```

They are part of the broad `hermes serve` desktop/dashboard surface rather than the narrow official API Server. Giving iOS its dashboard session token would grant substantially more authority than voice needs. TalkToHermes therefore uses a narrow bridge and local worker while preserving behavior through contract tests.

## Remaining custom policy

- fixed per-user bridge instance and token;
- configured GPU -> optional Wyoming -> local STT failover;
- OmniVoice -> optional Wyoming-Piper with an allowlisted per-instance voice -> local Piper TTS failover;
- OmniVoice omission/repetition validation;
- mobile upload/retry/idempotency and retention;
- native iOS recording, playback, and approval UI.

## Verified API nuance

`/v1/runs` with a repeated `session_id` persists messages but did not automatically load the preceding turn during the 2026-08-28 target test. `/api/sessions/{id}/chat/stream` does load history, but its generated run currently has no approval session resolvable through `/v1/runs/{id}/approval`. TalkToHermes therefore reads the canonical Hermes session messages immediately before each run and supplies them as the Runs API's documented `conversation_history`. A two-turn marker test proved continuity with four persisted messages; the idempotent replay did not create another turn.

## Provider provenance retained from implementation review

The OmniVoice service is an independent clean-room adapter to the Apache-2.0 `k2-fsa/OmniVoice` API. It neither copies nor depends on `scorbo2/ai-playground`; that reviewed wrapper and its related `TalkWithMe` client are MIT-licensed. The adapter pins `omnivoice==0.2.1` and immutable model revision `c5fdb5ccb189668d56333f77ba2629f4cd7535f4` in its deployment contract. The upstream wrapper exposes JSON/base64 `/synthesize`, not OpenAI binary `/audio/speech`, which is why direct Hermes OpenAI-TTS configuration against the legacy listener is incompatible.

The local fallback review used `piper-tts==1.7.0` from Open Home Foundation (`OHF-Voice/piper1-gpl`) with official `rhasspy/piper-voices` assets. The reviewed wheel SHA-256 was `72adc623b977bdbbdf3d6f6bf88d66eda7cfe2ee8e7919a74a5952acb77a339e`; the runtime dependency `pathvalidate==3.3.1` is MIT and its reviewed wheel SHA-256 was `5263baab691f8e1af96092fa5137ee17df5bdfbd6cff1fcac4d6ef4bc2e1735f`. Voice-specific model hashes and license notices must be reviewed with the selected deployment assets and kept with the deployment record rather than private voice configuration.

The dedicated STT service pins its offline `large-v3-turbo` model snapshot and validates media with PyAV before one serialized CUDA/float16 inference. It intentionally does not reuse the unauthenticated legacy listener on port `5005`, whose `response_format=text` behavior and development-server exposure were not OpenAI-compatible or production-hardened.

## Upgrade rule

Before implementing a new voice seam, check current official docs and upstream main. A weekly read-only review reports material changes. If the official API Server gains stable audio endpoints or Hermes ships a suitable iOS client, remove the corresponding custom component rather than maintaining a duplicate.
