# TalkToHermes Security Baseline

## Per-instance isolation

Every bridge process is bound permanently to one Unix user and one Hermes home/profile. Each instance has its own:

- external HTTPS port;
- internal loopback bridge and Hermes API ports;
- app token and Hermes API key;
- configuration, SQLite database, runtime/audio directory, and session mapping.

The client cannot submit `profile` or `instance_id`. Cross-instance tokens return `401`. State-directory reuse, unsafe ownership, or a port collision prevents startup.

## Authentication

- Hermes API uses a dedicated random key and stays on `127.0.0.1:<per-user-port>`.
- The iOS app uses a separate random bearer token stored in Keychain.
- Host and port are stored in app preferences; the bearer token is never stored there.
- The Keychain token is reused only for the unchanged normalized HTTPS host and port. Changing either requires explicit token entry before validation or commit.
- Authenticated redirects are followed only within the same HTTPS scheme, host, and port.
- Tokens never enter source, command arguments, screenshots, logs, or responses.
- Missing, reused, or weak secrets fail closed.
- Secret files are mode `0600` or systemd credentials.

## Network

- All instances use `hermes-agent.home.arpa` at `192.168.100.10` with distinct custom HTTPS ports.
- The native app contains no compiled installation endpoint. It accepts a hostname and port at runtime, constructs only HTTPS URLs, and rejects schemes, credentials, embedded ports, paths, queries, fragments, and invalid DNS labels.
- Caddy maps each external port to exactly one loopback bridge.
- `home.arpa` TLS uses Caddy's private PKI (`tls internal`); the internal Root CA is fingerprint-verified and explicitly trusted on managed iOS devices. No public ACME DNS provider or DNS API token is used for these names.
- Public routing, router forwarding, tunnel exposure, and public webhooks remain disabled.
- Direct-IP TLS is allowed only with a trusted certificate containing the private IP SAN. Verification is never bypassed.
- Hermes API, `hermes serve`, `/api/audio/*`, Wyoming, Coglet, OmniVoice, and Piper remain private backend seams.
- The OmniVoice service accepts only a configured RFC 1918 IPv4 listener on fixed port `9090`. `0.0.0.0`, loopback, public/documentation/invalid addresses, hostnames, port `8181`, and every other port fail closed before Uvicorn starts. Listener preflight binds exactly the validated tuple before startup.

## Input controls

Implemented voice limits:

```text
maximum upload: 10 MiB
maximum duration: 120 seconds
accepted containers: m4a, caf, wav, ogg
```

The bridge verifies media, ignores supplied filenames, generates paths, invokes subprocesses without a shell, bounds output/stderr, and kills complete process trees on timeout.

The bounded voice worker validates interpreter, virtual-environment boundary, Hermes root and worker script ownership/modes before spawn and fingerprints them against replacement. It opens the interpreter and worker without following symlinks and executes them through inherited `/proc/self/fd` descriptors in isolated Python mode with a minimal environment. OmniVoice reference audio and transcripts are private server files; retained descriptors prevent path-replacement races, and requests can select only logical allowlisted voice IDs.

## Tool approvals

MVP decisions are only:

- `once`: approve the concrete pending action once;
- `deny`: reject it.

`session` and `always` are unavailable. Approval remains visible when chat text is hidden. Voice-only approval is rejected. Missing or expired approval resolves to deny.

## Data retention

- Failed diagnostic uploads are retained for at most 24 hours by default.
- `retain_failed_audio=false` removes failed uploads immediately.
- Completed and cancelled uploads are removed immediately; restart-interrupted work is never retained.
- Undownloaded answer audio expires after 24 hours. Its first authenticated GET starts one fixed five-minute default retry/reconnect lease; retries do not extend it.
- Voice input transcripts and answer text remain locally readable for at most 24 hours after a turn becomes terminal. Cleanup then redacts both text fields and removes text-bearing `hermes.delta` events while retaining content-free turn metrics and provider-attempt events. Active turns are never redacted.
- Cleanup runs at startup and periodically, is idempotent, stays directly inside the instance audio root, and never follows symlinks or removes wrong-owner/mode entries.
- The dedicated Hermes session remains the longer-lived canonical conversation context. Local text redaction and TalkToHermes conversation deletion do not currently delete that Hermes session.
- Text visibility is a client presentation setting and does not alter the Hermes session or the bounded local retention rule.
- Clone references, generated audio, tokens, databases, and private voices remain outside Git.

## Logging

Allowed: opaque request/turn IDs, states, durations, selected provider names, token counts, and bounded error codes.

Forbidden:

- credentials or authorization headers;
- raw audio, clone references, transcripts by default;
- full tool payloads/results;
- filesystem paths returned to clients;
- unredacted model/provider exceptions.

## Deployment permissions

Each bridge and Hermes API process runs as its owning unprivileged Unix user. Shared root-owned voice adapters may be read/executable only. Deployment receives only explicitly enumerated commands. No general sudo shell, wildcard command, interpreter grant, Home Assistant write permission, or remote root key.
