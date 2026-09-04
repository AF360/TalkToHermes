# TalkToHermes Architecture

![TalkToHermes architecture](/images/TalkToHermes-architecture.png)

## Goal

TalkToHermes is a private native iPhone/iPad push-to-talk frontend for a Hermes agent. It uses each operating-system user's own Hermes identity, memory, skills, tools, sessions, and approvals. It does not add a separate persona model.

## Reuse before custom code

Hermes already provides voice recording behavior, VAD, sentence-aware TTS, local STT recovery, Piper, OpenAI-compatible TTS, sessions, runs, SSE, stop, and approvals. TalkToHermes adds only the missing mobile client, narrow LAN boundary, provider policies, retention, and OmniVoice quality checks.

The broad Hermes Desktop/dashboard API is not exposed to iOS. In particular, the app receives no dashboard token and cannot call `/api/ws` or `/api/audio/*` directly.

## Component topology

```text
iPhone/iPad
  -> HTTPS: hermes-agent.home.arpa:<instance-port>
  -> Caddy on 192.168.100.10
  -> one TalkToHermes Voice Bridge instance (loopback)
       -> fixed OS user/profile and isolated state
       -> ordered STT providers from the instance YAML
            1. OpenAI-compatible GPU STT: primary-voice-server.home.arpa / 192.168.100.20
            2. optional Wyoming fallback: fallback-voice-server.home.arpa / 192.168.100.30
            3. configured Hermes local model
       -> official Hermes API Server on a per-user loopback port
            -> dedicated persistent Hermes session
       -> ordered TTS providers from the instance YAML
            1. HTTPS OmniVoice on primary-voice-server.home.arpa
            2. optional Wyoming-Piper on fallback-voice-server.home.arpa with a dedicated warm per-voice port
            3. Hermes Voice Worker local Piper with configured voice
  <- buffered answer audio plus optional text
```

## Multi-instance model

Each human gets a separate process, not a profile parameter on a shared process.

```text
talktohermes@instance-a
  fixed user/home: instance-a / /home/instance-a/.hermes
  dedicated external HTTPS port
  dedicated bridge and Hermes loopback ports
  dedicated app token, Hermes key, DB, audio and session mapping

talktohermes@instance-b
  fixed user/home: instance-b / /home/instance-b/.hermes
  dedicated external HTTPS port
  dedicated bridge and Hermes loopback ports
  dedicated app token, Hermes key, DB, audio and session mapping
```

Both use `hermes-agent.home.arpa`; host plus port identifies the instance. Caddy maps every external port statically to one loopback bridge. Requests cannot select `profile` or `instance_id`.

## Trust boundaries

1. iOS reaches only its narrow Voice Bridge instance.
2. Every Hermes API Server remains loopback-only and has a separate key.
3. App token and Hermes key are different credentials per instance.
4. Wyoming, Coglet, OmniVoice, Piper, Hermes API, and `hermes serve` are never directly exposed to iOS.
5. No router forwarding, public tunnel, or cloud proxy.
6. Caddy issues `home.arpa` certificates through its private PKI (`tls internal`); managed iOS clients explicitly trust the fingerprint-verified internal Root CA.

## Session model

One TalkToHermes conversation maps to one dedicated Hermes session in the configured user's profile. The mapping is server-side; Hermes session IDs are not exposed. TalkToHermes conversations do not merge with Telegram transcripts.

The Hermes session is the longer-lived canonical conversation context. Deleting a TalkToHermes conversation removes its local mapping and artifacts but intentionally does not yet delete the mapped Hermes session.

The verified approval-capable turn path composes two official APIs. Before each run the bridge reads `/api/sessions/{id}/messages` and passes that canonical history as `conversation_history` to `/v1/runs` together with the same `session_id`. This is necessary because current Runs persist but do not automatically resume prior session context, while session-chat streaming currently lacks a run approval session. The bridge never owns a second conversation-history store.

Each run also receives a bounded, operator-configurable `hermes.voice_instructions` overlay. It preserves the configured Hermes profile, identity, memory, tools and safety prompt while shaping output for spoken delivery. The authenticated client selects only the allowlisted per-turn response style `short`, `normal` or `detailed`; the bridge maps that enum to fixed instructions and fingerprints it as part of idempotency. No request can supply arbitrary system-prompt text.

The authenticated status response exposes the configured bridge `instance_id` and the explicit server-controlled `assistant_name` (a printable, unpadded display name of 1–64 Unicode characters). The native client treats endpoint plus `instance_id` plus `assistant_name` as the bridge identity and resets local conversation state when any of them changes. It displays the supplied assistant name instead of deriving a persona from an identifier. Spoken-conversation instructions remain operator-configurable per bridge and are never supplied by the client.

Tool activity is fail-closed metadata. The bridge exposes only internal tool IDs present in the per-instance `exposed_tools` mapping, translates them to constrained alphanumeric display identifiers, deduplicates them, and never forwards tool arguments, previews, results, paths, or prompts. An unmapped tool produces no client-visible metadata.

## Hermes Voice Worker

The bridge owns no copied provider implementation. A small subprocess in the configured Hermes venv exposes a bounded JSON stdin/stdout contract:

```text
tts(piper)  -> text_to_speech_tool(provider="piper")
stt-local   -> transcribe_audio_local_fallback()
normalize   -> prepare_spoken_text()
omnivoice   -> text_to_speech_tool(provider="openai")
               -> separate primary voice server wrapper /v1/audio/speech
```

This isolates Python dependencies and lets contract tests detect upstream changes. The existing fallback voice server adapter and primary voice server's OpenAI-compatible STT endpoint remain explicit bridge adapters. OmniVoice uses a separate TalkToHermes-specific service and port on primary voice server; TalkWithMe's service on port 8181 remains untouched. No global Hermes voice configuration is changed.

## Availability modes

| Mode | STT | TTS |
|---|---|---|
| Normal | first configured provider | first configured provider |
| Primary off | next configured provider | next configured provider |
| Optional fallback absent/off | configured local model | configured local Piper voice, degraded mode |

Remote HTTPS providers use independent connection and response deadlines. The
default 0.5-second connection bound covers DNS, TCP, and TLS establishment while
the default 120-second response bound still permits bounded model inference. A
connection refusal, unreachable network, connect timeout, or failed TLS setup
opens a process-local circuit for that validated endpoint for 45 seconds. Calls
during the cooldown do not open a socket; after expiry exactly one real request
is admitted half-open. A reachable completion closes the circuit and restores
the configured primary-first order, while another connectivity failure reopens
it. No ICMP ping or readiness preflight is used.

OmniVoice connectivity failure is not retried and advances directly to the next
configured TTS provider. A WAV that was actually synthesized but rejected by
bounded quality verification retains one quality retry. Cancellation always
propagates and is never converted into fallback. Attempt events expose only the
logical provider, outcome/error code, elapsed milliseconds, circuit state, and
selected fallback; they never contain text, transcripts, audio, credentials,
authorization headers, endpoint URLs, or private paths.

The fully local `hermes-agent.home.arpa` path is explicit degraded operation: local STT and Piper worker calls share one process-wide, event-loop-neutral concurrency slot. A turn exposes `degraded_local_audio=true` if either final STT or TTS used that local path.

## Voice turn and audio lifecycle

The authenticated multipart route drives the persisted state machine `accepted -> transcribing -> thinking -> awaiting_approval|synthesizing -> completed|failed|cancelled`. It fingerprints audio plus language, voice, text-visibility and response-style options under the client UUID. Exact retries return the same turn; changed content or options return `409` without starting a second Hermes run. Redacted events have atomic per-turn sequence numbers and `Last-Event-ID` replays only newer events. The bridge stores the voice input transcript and answer text only as a bounded local presentation cache; Hermes remains the canonical conversation store.

Uploads are streamed with a 10 MiB limit, accept WAV, M4A/MP4, CAF or Ogg input with a validated language tag, verify container signatures, and enforce a 120-second duration cap. Supplied filenames are ignored. Cancel stops a known Hermes run, cancels and awaits active STT/TTS work, and then cleans artifacts. Startup converts interrupted active turns to a bounded terminal error and resumes durable conversation deletions before normal recovery.

Completed and cancelled uploads are removed immediately. Failed diagnostic uploads may be retained for at most 24 hours or removed immediately by configuration. Undownloaded answer audio expires after 24 hours; its first authenticated download starts one fixed five-minute default reconnect lease that retries do not extend. Voice input transcripts and answer text remain locally readable for no more than 24 hours after the turn reaches a terminal state, then both fields are redacted and text-bearing `hermes.delta` events are removed; content-free turn metrics and safe provider-attempt events remain. Active turns are not redacted. Cleanup is idempotent, does not follow symlinks, and touches only safe regular files directly below the instance audio root. One process exclusively owns each SQLite database.

### primary voice server STT contract

The dedicated primary voice server exposes `POST /v1/audio/transcriptions` through an OpenAI-style multipart adapter (`file`, `model`, `language`, `response_format=json`). It is an explicit STT rung and does not change Hermes' global STT provider. The dedicated service uses loopback behind Caddy, bearer authentication, a 10 MiB request limit, validated media, serialized GPU inference, minimal health output, and production process supervision.

## TTS integrity

Hermes' spoken-text normalization and sentence behavior are reused. OmniVoice segments may be back-transcribed to detect omissions, repetitions, empty output, and implausible duration. One quality-rejected synthesized segment gets at most one OmniVoice retry; connectivity, timeout, HTTP, format, output, and verifier-technical failures advance directly to the next configured provider. Repeated quality rejection regenerates the entire response with the next configured provider, avoiding a voice change mid-answer. For conversational latency, use one warm Wyoming-Piper process and port per remote voice. That server deployment is deliberately separate from this configuration refactor.

## Deferred capabilities and future considerations

- Wake word and always-on microphone
- Streaming audio
- Public internet access
- TestFlight/App Store distribution
- Shared multi-user process
- `session` or `always` approvals
- Hermes core modifications

## Native client behavior and build boundary

The iOS 17+ SwiftUI client accepts a bridge hostname, HTTPS port, and bearer token at runtime; no installation-specific endpoint is compiled into the app. HTTPS is fixed. The client trims and lowercases the hostname, removes a trailing dot, validates DNS labels and the port range, and rejects URL schemes, credentials, embedded ports, paths, queries, and fragments. It verifies authenticated `GET /v1/status` before committing settings.

The bearer token is stored with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`. An empty token field may reuse the Keychain token only when the normalized host and port are unchanged. Changing either value requires an explicitly entered token, and failed validation leaves the previous endpoint and token intact. Authenticated redirects are accepted only within the same HTTPS scheme, host, and port. A saved endpoint change rebuilds the API client and clears the current conversation binding so a conversation is not silently reused against another bridge. Normal URLSession certificate validation remains mandatory; no TLS bypass exists.

Voice uploads carry an explicit language tag. The native client stores an independent spoken-language choice (`de` or `en`), defaults missing or invalid legacy settings to German, and sends the selected value explicitly on every turn; it does not infer speech language from app localization, device language, or region. Recording is tap-to-start/tap-to-send, playback can be stopped and replayed, and tapping the microphone during playback stops audio and starts a new recording without deleting conversation context. If recording startup fails, the previous answer remains replayable. Build and endpoint setup are documented in [`ios/README.md`](../ios/README.md).
