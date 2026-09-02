# TalkToHermes

![TalkToHermes](/images/TalkToHermes.png)

Private native voice and optional-text frontend for a Hermes agent on iPhone and iPad.

**Multilingual:** `en` / `de` provided. The app language and spoken voice language can be selected independently.

## Architecture

```text
iPhone/iPad
  -> hermes-agent.home.arpa:<per-instance HTTPS port>
  -> narrow per-instance TalkToHermes Voice Bridge
  -> official Hermes sessions/runs API on loopback
  -> ordered voice providers through bounded adapters
```

Each user has a separate process, token, port, Hermes home, database, audio store, and session mapping. The app selects a configured bridge endpoint but cannot ask that bridge to switch profile or instance.

## Provider policy

```text
STT: ordered per-instance providers (OpenAI-compatible -> optional Wyoming -> local)
TTS: ordered per-instance providers (OmniVoice -> optional Wyoming-Piper -> local Piper)
```

List order is fallback order. Omit an optional middle provider to fall straight through to the configured local last resort. Only local STT/Piper is marked as locally degraded. OmniVoice listens on a configured RFC 1918 IPv4 address and the fixed dedicated port `9090`; wildcard, loopback, public or invalid addresses and every other port are rejected at configuration load and listener preflight.

## Status

The bridge and native voice path implement bounded private upload, STT fallback, official Hermes run/SSE/approval/cancel, whole-answer quality-orchestrated TTS, authenticated audio delivery, restart recovery, and bounded retention. Dedicated STT (`9444`) and OmniVoice (`9443`) examples use authenticated TLS on `primary-voice-server.home.arpa`; the legacy shared STT listener on `5005` is not a TalkToHermes production target. The SwiftUI client provides Keychain-backed authentication, transactional settings, replay/stop and tap-to-interrupt recording. The authenticated bridge status supplies the configured instance identity to the client. A bounded operator-configurable voice overlay preserves the selected Hermes identity and safety prompt, while the client can select only `short`, `normal`, or `detailed`.

## Canonical documents

- [Architecture](docs/architecture.md)
- [Security baseline](docs/security.md)
- [iOS client setup and endpoint configuration](ios/README.md)
- [Production deployment and operations](deployment/README.md)
- [API contract](api/openapi.yaml)
- [OmniVoice service](services/omnivoice/README.md)
- [STT service](services/stt/README.md)
- [Voice reuse and provenance](docs/upstream-voice-reuse.md)
- [License](LICENSE)
