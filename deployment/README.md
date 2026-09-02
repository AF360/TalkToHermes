# Production deployment (Debian 13 / systemd 257)

This directory is an operator-reviewed example, not an installer. It makes no production changes. Each instance is one existing Unix account, one loopback bridge port, two bridge credentials plus any tokens required by configured remote providers, and one SQLite state directory. Never run two bridge processes against the same state directory.

The bridge remains a systemd service. Caddy remains the existing Docker deployment. The Caddy example deliberately does **not** install a systemd Caddy, replace a complete Caddyfile, or add a cloud tunnel/public proxy.

## Prerequisites

This repository is not a host bootstrapper. Provision and verify the required operating-system, Hermes, voice-runtime, model, and TLS components before running the deployment scripts. Upstream installation examples are not project version pins: the committed lockfiles and service-specific runtime contracts take precedence for a reviewed TalkToHermes revision.

### Required on the bridge host

- Debian with systemd user services, one dedicated Unix account per bridge instance, and `systemctl --user` plus `systemd-analyze`. Enable user lingering separately if an instance must start without an interactive login.
- Operator tools used by the scripts and gates: `git`, `curl`, `ca-certificates`, `coreutils`, `iproute2`, and an administrator-reviewed [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installation. Install bridge dependencies from `backend/uv.lock` with `uv sync --frozen`.
- An installed [Hermes Agent](https://hermes-agent.nousresearch.com/docs/getting-started/installation/) for each instance account. Its API must already be healthy on loopback and the configured `voice_worker` paths must point to that account's Hermes interpreter and root. TalkToHermes does not install or replace Hermes or its Telegram gateway.
- A reviewed TalkToHermes checkout, private per-instance configuration and secrets, a mode-`0700` state directory, and the Caddy/private-PKI setup documented below.

### Voice-provider prerequisites

Provision only providers present in an instance's ordered `stt` and `tts` lists. The first entry is preferred; later entries are fallbacks.

#### OmniVoice TTS

Required when `type: omnivoice` is configured. Use the official [OmniVoice repository](https://github.com/k2-fsa/OmniVoice) and [`k2-fsa/OmniVoice` model](https://huggingface.co/k2-fsa/OmniVoice). Prepare the host in this order:

1. Install and verify the selected accelerator runtime and matching PyTorch build using the [PyTorch installation selector](https://pytorch.org/get-started/locally/).
2. Create a clean Python environment compatible with `services/omnivoice/pyproject.toml` and install the project from `services/omnivoice/uv.lock`; do not layer an unbounded second `pip install omnivoice` over it.
3. Pre-populate the pinned model revision in the service account's Hugging Face cache and prepare private reference WAV/transcript pairs for every configured logical voice. OmniVoice accepts language names and codes; this project uses codes such as `de` or `en` in examples.
4. Verify model loading and one real synthesis using the intended accelerator, dtype, voice assets, and offline settings.
5. Only then install the TalkToHermes service described in [`services/omnivoice/README.md`](../services/omnivoice/README.md). `/usr/bin/ffmpeg` is additionally required for MP3 output.

#### Whisper STT

- The dedicated primary STT service requires [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper), PyAV, a pre-populated model cache, and a compatible CPU or NVIDIA/CUDA/cuBLAS/cuDNN runtime in `/opt/stt`. Follow current Faster-Whisper/CTranslate2 compatibility notes and verify one representative transcription before installing the wrapper in [`services/stt/README.md`](../services/stt/README.md). Faster-Whisper decodes through PyAV and does not itself require a system `ffmpeg` executable.
- [`mlx-whisper`](https://github.com/ml-explore/mlx-examples/tree/main/whisper) is an optional Apple-Silicon local STT implementation. Install it only in the Hermes environment that performs the local fallback, cache the selected model, install `ffmpeg`, and verify transcription from that same account.

#### Piper and Wyoming services

- Install [Piper](https://github.com/OHF-Voice/piper1-gpl) wherever `local-piper` or Wyoming-Piper runs. Every configured voice needs its matching `.onnx` model and `.onnx.json` configuration; review the official [Piper voice catalogue and download instructions](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md), model card, and license. Installing Piper does not automatically install the selected voice.
- [`wyoming-piper`](https://github.com/rhasspy/wyoming-piper) is an optional warm network TTS provider. For conversational latency, use one persistent process and private port per voice.
- [`wyoming-faster-whisper`](https://github.com/rhasspy/wyoming-faster-whisper) is an optional network STT provider. Cache its model before offline operation. The repository's Wyoming STT adapter also requires the Wyoming Python package and `ffmpeg` in its dedicated environment.
- Wyoming transport is unencrypted TCP. Bind it only to the reviewed private interface, restrict it with the site firewall, and never expose it to the Internet.

### Media, GPU, and TLS

- Do not install a second CUDA stack when a reviewed voice environment already works. Independently verify NVIDIA driver visibility, PyTorch compatibility, CTranslate2 compatibility, library paths, model loading, and real inference. A working PyTorch CUDA environment does not prove Faster-Whisper compatibility, or vice versa.
- Install [`ffmpeg`](https://ffmpeg.org/download.html) on hosts that perform OmniVoice MP3 conversion, use MLX-Whisper, or run the repository's Wyoming STT adapter.
- The documented topology requires Caddy for app-facing and primary-voice-server HTTPS. The `home.arpa` examples use [Caddy local HTTPS](https://caddyserver.com/docs/automatic-https#local-https) with `tls internal`; persist its data, fingerprint the public Root CA, and trust it on the bridge and managed clients. Never transfer the CA private key or bypass verification with `curl -k`.
- Model weights, voice files, caches, tokens, and reference audio are runtime data outside immutable repository releases and must be owned by the dedicated service account.

## Deployment scripts

Three transactional scripts cover the three runtime subsystems. They validate prerequisites before replacing files, keep a rollback copy until the real health check passes, and never carry credentials in command arguments:

- `deployment/scripts/deploy-hermes-agent-user.sh INSTANCE [REVISION]` — run from a checkout containing the requested revision as the unprivileged bridge user. It creates an immutable Git-archive release, installs the systemd user unit, restarts the named bridge, and verifies its loopback health endpoint.
- `deployment/scripts/deploy-primary-voice-server-user-services.sh [REPOSITORY_ROOT] [VOICE_HOST_IP] [REVISION]` — run on the GPU voice host as its unprivileged service user. It deploys both service packages and units from one resolved Git commit, restarts them sequentially, and verifies both health endpoints. Existing model caches, tokens, venvs, and configuration remain outside the replaced package directories. The documented example address defaults to `192.168.100.20`; private deployments pass their own LAN address explicitly.
- `deployment/scripts/deploy-fallback-piper-user.sh INSTANCE VOICE PORT BIND_IP [SERVER_ROOT]` — run in the target macOS GUI session as the voice-service user. It installs a launchd agent and the warm-Piper supervisor, validates the selected local model, rejects occupied ports, performs an automatic synthesis warm-up, and rolls back if readiness fails.

The scripts intentionally do not disable unrelated legacy root daemons. Retiring one requires a separately reviewed privileged `launchctl bootout system/LABEL` and preserving its plist for rollback.

## Preferred bridge user-service installation

The current deployment runs the bridge in the instance user's systemd manager. It needs no root-owned release, configuration, or state directory. Prepare it as the instance user from a repository checkout:

```sh
INSTANCE=instance-a
install -d -m 0700 "$HOME/.config/talktohermes" \
  "$HOME/.local/state/talktohermes/$INSTANCE"
cp deployment/config/instance.user.yaml.example \
  "$HOME/.config/talktohermes/$INSTANCE.yaml"
cp deployment/config/instance.secrets.example \
  "$HOME/.config/talktohermes/$INSTANCE.secrets"
chmod 0600 "$HOME/.config/talktohermes/$INSTANCE.secrets"
```

Replace every literal `INSTANCE` in the YAML with the dedicated Unix account, set that bridge's ordered providers and loopback port, and enter the four independent secret values in the `0600` secrets file. Do not put credentials in YAML or command arguments. Then deploy one reviewed Git revision:

```sh
deployment/scripts/deploy-hermes-agent-user.sh "$INSTANCE" REVISION
systemctl --user is-enabled "talktohermes@$INSTANCE.service"
systemctl --user is-active "talktohermes@$INSTANCE.service"
```

For startup without an interactive login, an administrator performs the one-time host operation `loginctl enable-linger INSTANCE`. The user unit deliberately omits mount-namespace and cgroup-IP-firewall directives: an unprivileged user manager maps reviewed root-owned adapter paths to `nobody` when creating that namespace, causing the bridge's root-ownership gate to reject them. The process still runs as the dedicated unprivileged account with the remaining compatible restrictions.

The root-owned `/opt`, `/etc`, `/var/lib`, and system-manager procedure below is retained as a separately scoped legacy alternative. Do not mix its paths or unit with the preferred user-service layout.

## Invariants and assumptions

- `User=%i` and `Group=%i`: the instance name is an existing dedicated Unix account and valid bridge `instance_id`. If it does not exist, an administrator may create it with `useradd --create-home --shell /usr/sbin/nologin INSTANCE`; do not repurpose an unrelated account.
- Root owns releases below `/opt/talktohermes/releases`; `/opt/talktohermes/current` is a root-owned symlink. Runtime users cannot change bridge code.
- `/etc/talktohermes/INSTANCE.yaml` is root-owned mode `0644` and contains no credentials. `/etc/talktohermes/INSTANCE.secrets` is owned by that instance and mode `0600`, because the unprivileged process must read it. The enclosing directory is root-owned mode `0755`.
- `/var/lib/talktohermes/INSTANCE` is owned by the instance and mode `0700`. It must never be shared across instances.
- The Hermes gateway/API stays at `http://127.0.0.1:8642`. Preserve the existing Hermes/Telegram gateway; do not rebind, replace, disable, or expose it. The TalkToHermes bridge also stays on `127.0.0.1`.
- `hermes.voice_instructions` is a non-secret, bounded instance setting appended by Hermes to its normal system prompt for each TalkToHermes run. Keep it voice-oriented and free of credentials. Clients may choose only `short`, `normal`, or `detailed`; they cannot submit arbitrary instructions.
- The worker paths are `/home/INSTANCE/.hermes/hermes-agent/venv/bin/talktohermes-python`, root-owned `/opt/talktohermes/current/backend/worker/hermes_voice_worker.py`, and `/home/INSTANCE/.hermes/hermes-agent`. The Hermes root/venv/interpreter remain instance-owned and read-only to the service mount namespace; the immutable release script may be root-owned. `ProtectHome=tmpfs` exposes only the explicitly read-only `.hermes` tree and the uv-managed Python base tree referenced by that venv; no home writes are permitted. The worker's `/proc/self/fd` launch is an activation test gate under `ProtectProc=invisible`/`ProcSubset=pid`.
- Provider order comes directly from each instance's `stt` and `tts` lists. HTTPS and `tcp://` targets, remote voices, the local STT model, and the local Piper voice are instance settings. Omitting an optional middle provider makes failure fall directly to the local last resort.
- Remote HTTPS entries independently configure `connect_timeout_seconds` (default `0.5`, range `0.1`–`5`), `response_timeout_seconds` (default `120`, range `0.1`–`300`), and `circuit_cooldown_seconds` (default `45`, range `5`–`300`). Keep the connect bound short on the private LAN without reducing the bounded inference deadline. The bridge uses real TCP/TLS requests, never ICMP ping, and skips an unavailable endpoint during its process-local cooldown before allowing one half-open recovery request. Duplicate remote endpoints in one STT or TTS list are rejected so a cooldown cannot be bypassed by a second list entry.
- `text_retention_hours` defaults to and cannot exceed `24`. It applies to locally cached voice input transcripts, answer text, and text-bearing SSE deltas after a turn becomes terminal; content-free metrics and provider events remain. `cleanup_interval_seconds` is a discovery heartbeat from `1` through `900` seconds. Once a pending text expiry is known, the process schedules cleanup for that deadline rather than waiting for the next full heartbeat. A transient cleanup failure is retried without terminating the retention task.
- The public example LAN uses `192.168.100.10` (`hermes-agent.home.arpa`), `.20` (`primary-voice-server.home.arpa`), and optional `.30` (`fallback-voice-server.home.arpa`). These are documentation examples only. The main systemd unit permits only loopback; site-local resolver and provider `/32` addresses belong in the operator drop-in described below.
- For conversational latency, run one warm Wyoming-Piper process per voice on a dedicated port (for example `:10201` and `:10202`) and point each bridge at its process. This deployment on the fallback voice server is intentionally deferred; the configuration work does not start or modify those services.
- One public **HTTPS** custom port maps to one unique loopback bridge port. No HTTP listener is defined.

## Minimal privileged commands

Only an administrator runs these classes of commands; application tests/builds need no privilege:

```text
useradd ...                                      # only if the dedicated user is absent
install/chown/chmod under /opt, /etc, /var/lib
ln -sfn under /opt/talktohermes
systemctl daemon-reload|enable|restart|stop
docker inspect|build|compose|exec                 # existing Docker Caddy operator
firewall change for the selected HTTPS port       # only if policy requires it
```

The normal operator here has neither passwordless sudo nor Docker-group access. Do not work around that; hand the reviewed commands/artifacts to the privileged operator.

## Port collision preflight

Choose unique values, for example bridge `18081` and public HTTPS `8443`, then check **before any write**:

```sh
ss -ltnp 'sport = :18081'
ss -ltnp 'sport = :8443'
```

An empty result is required for a new instance. For an upgrade, the known `talktohermes@INSTANCE.service` may own the bridge port. Any other owner is a blocker. Also confirm the chosen ports differ from Hermes `8642`, Caddy `80/443`, and Caddy admin `2019`.

## Legacy root/system-service install

1. As an unprivileged release builder, test and build from the reviewed revision:

   ```sh
   cd backend
   .venv/bin/pytest -q
   uv lock --check
   ```

2. As administrator, stage a collision-free immutable archive of the reviewed Git object (replace `REVISION` with the full reviewed commit). This does not copy a developer `.venv`. Use the committed `backend/uv.lock`; no unconstrained dependency install is allowed:

   ```sh
   test "$(git rev-parse REVISION^{commit})" = "REVISION"
   install -d -o root -g root -m 0755 /opt/talktohermes/releases/REVISION
   git -c tar.umask=0022 archive --format=tar REVISION | tar -x -C /opt/talktohermes/releases/REVISION
   uv sync --frozen --no-dev --no-editable --project /opt/talktohermes/releases/REVISION/backend
   chown -R root:root /opt/talktohermes/releases/REVISION
   ln -sfn /opt/talktohermes/releases/REVISION /opt/talktohermes/current
   ```

   The explicit archive umask is a security requirement: `git archive` otherwise inherits the builder's configured tar umask, and root extraction preserves group-writable modes that the worker-path gate correctly rejects. Verify the extracted worker directory is `0755` and `hermes_voice_worker.py` is `0644` before activation.

   `uv` itself must be an administrator-reviewed pinned installation. If the target is offline, pre-populate the reviewed uv cache and add `--offline`; do not remove `--frozen` or fall back to network-resolved `pip install`.

3. Copy `config/instance.yaml.example`, replace every `INSTANCE`, select the checked bridge port, and set the ordered provider lists. Keep `development: false`; only the bridge and Hermes URLs are loopback. Omit unavailable optional fallback providers instead of configuring dummy endpoints:

   ```sh
   install -d -o root -g root -m 0755 /etc/talktohermes
   install -o root -g root -m 0644 INSTANCE.yaml /etc/talktohermes/INSTANCE.yaml
   install -d -o INSTANCE -g INSTANCE -m 0700 /var/lib/talktohermes/INSTANCE
   ```

4. Create the secret file without credentials in command arguments or shell history. Start from an empty protected file, enter values with silent `read`, and delete shell variables immediately:

   ```sh
   install -o INSTANCE -g INSTANCE -m 0600 /dev/null /etc/talktohermes/INSTANCE.secrets
   umask 077
   read -r -s -p 'APP_TOKEN: ' APP_TOKEN; printf '\n'
   read -r -s -p 'HERMES_API_KEY: ' HERMES_API_KEY; printf '\n'
   read -r -s -p 'STT_PRIMARY_TOKEN: ' STT_PRIMARY_TOKEN; printf '\n'
   read -r -s -p 'TTS_PRIMARY_TOKEN: ' TTS_PRIMARY_TOKEN; printf '\n'
   printf 'APP_TOKEN=%s\nHERMES_API_KEY=%s\nSTT_PRIMARY_TOKEN=%s\nTTS_PRIMARY_TOKEN=%s\n' \
     "$APP_TOKEN" "$HERMES_API_KEY" "$STT_PRIMARY_TOKEN" "$TTS_PRIMARY_TOKEN" \
     > /etc/talktohermes/INSTANCE.secrets
   unset APP_TOKEN HERMES_API_KEY STT_PRIMARY_TOKEN TTS_PRIMARY_TOKEN
   chown INSTANCE:INSTANCE /etc/talktohermes/INSTANCE.secrets
   chmod 0600 /etc/talktohermes/INSTANCE.secrets
   ```

   All four tokens must be unique, unrelated, 32–256 characters, and use only letters, digits, `_`, or `-`. Never paste them into YAML, Compose, unit files, tickets, logs, or command arguments. If an instance uses only local providers, the unused primary-provider token lines may be omitted.

### Optional Wyoming STT adapter

Only instances whose `stt` list contains `type: wyoming` need this adapter. Stage the reviewed wrapper at the exact path used by the bridge; this is a future operator step, not something performed by the configuration refactor:

```sh
install -d -o root -g root -m 0755 /opt/hermes-stt-wyoming/app
uv venv --python /usr/bin/python3.13 /opt/hermes-stt-wyoming/venv
uv pip install --python /opt/hermes-stt-wyoming/venv/bin/python 'wyoming==1.10.0'
install -o root -g root -m 0644 \
  deployment/wyoming/wyoming_stt.py \
  /opt/hermes-stt-wyoming/app/wyoming_stt.py
command -v ffmpeg
```

Review and lock the `uv` provenance before running these commands. The adapter receives the configured `tcp://` URI through `--uri`; no private host is compiled into it. An instance without a Wyoming STT entry neither validates nor invokes this path.

5. Install the unit, but do not activate it until its site-local egress drop-in is complete. The main unit is deliberately fail closed: `IPAddressDeny=any` plus loopback only. Copy `systemd/talktohermes@.service.d/egress.conf.example` to `/etc/systemd/system/talktohermes@INSTANCE.service.d/egress.conf`, resolve every configured provider hostname using the target host's actual DNS setup, and replace `DNS_RESOLVER_IP`, `PRIMARY_VOICE_SERVER_IP`, and—only when configured—`FALLBACK_VOICE_SERVER_IP` with the verified addresses. Remove unused provider lines. Never guess a resolver address or leave a placeholder.

   ```sh
   install -o root -g root -m 0644 systemd/talktohermes@.service /etc/systemd/system/talktohermes@.service
   install -d -o root -g root -m 0755 /etc/systemd/system/talktohermes@INSTANCE.service.d
   install -o root -g root -m 0644 systemd/talktohermes@.service.d/egress.conf.example /etc/systemd/system/talktohermes@INSTANCE.service.d/egress.conf
   ! grep -Eq 'DNS_RESOLVER_IP|PRIMARY_VOICE_SERVER_IP|FALLBACK_VOICE_SERVER_IP' \
     /etc/systemd/system/talktohermes@INSTANCE.service.d/egress.conf
   systemd-analyze verify /etc/systemd/system/talktohermes@.service \
     /etc/systemd/system/talktohermes@INSTANCE.service.d/egress.conf
   systemctl daemon-reload
   systemctl show talktohermes@INSTANCE.service -p IPAddressDeny -p IPAddressAllow
   systemctl enable talktohermes@INSTANCE.service
   systemctl restart talktohermes@INSTANCE.service
   systemctl is-active talktohermes@INSTANCE.service
   ```

   Compare the effective `systemctl show` allowlist with the separately recorded resolver/provider resolutions, then verify DNS resolution and each configured provider through the running service. Any placeholder, missing resolver/provider `/32`, extra address, failed resolution, or failed reachability is an activation blocker: keep or return the service to stopped state, correct the drop-in, and repeat all checks. This fail-closed drop-in verification is mandatory. If `systemd-analyze` is unavailable in a packaging/test environment, the unit tests remain mandatory and the real target must perform verification before activation.

## Existing Docker Caddy integration

The production root is `/opt/caddy`; host networking and persistent `/data` and `/config` volumes are in use. The legacy OpenClaw container and floating image were retired. Preserve the established container name `caddy-hermesagent` and image identifier `local/caddy-hermesagent:2.11.4-cloudflare-a8737d095ad5`; renaming either is a separate migration. The container serves the authenticated Hermes dashboard from `127.0.0.2:9119` on standard HTTPS, while the Hermes gateway API remains private on `127.0.0.1:8642`. TalkToHermes has a separate `8443 → 127.0.0.1:18081` route.

```sh
docker inspect --format '{{.Config.Image}}' caddy-hermesagent
docker exec caddy-hermesagent caddy version
docker exec caddy-hermesagent caddy list-modules
```

The established reviewed image includes Caddy 2.11.4 with pinned builder/runtime indexes and the historically bundled DNS module. The `home.arpa` routes do not invoke a public DNS provider and require no DNS API token; they use Caddy's private PKI with `tls internal`. Do not change the image identity in this repository scrub. Build from `/opt/caddy` after copying in the reviewed Dockerfile and merging—not replacing—the Compose service:

```sh
docker compose -f docker-compose.yaml build --pull=false caddy
docker compose -f docker-compose.yaml run --rm caddy caddy version
docker compose -f docker-compose.yaml run --rm caddy caddy list-modules
```

Merge, do not blindly apply, `caddy/compose.example.yaml` into `/opt/caddy/docker-compose.yaml`. Preserve the existing Hermes Agent route and `/data`/`/config` storage. Keep host networking. No DNS-provider secret mount or environment variable is needed for these `home.arpa` routes.

Merge the reviewed `caddy/Caddyfile.merged.example` with `/opt/caddy/Caddyfile` by diff. Both `home.arpa` sites explicitly use `tls internal`. Standard HTTPS proxies only to the authenticated dashboard on `127.0.0.2:9119`; never proxy the private Hermes API on `127.0.0.1:8642`. TalkToHermes uses the same hostname on port `8443` and upstream `127.0.0.1:18081`.

Before restart, validate the complete merged Caddyfile and prove both port-specific routes are still present in adapted JSON. After restart, verify valid TLS and upstream behavior for both `https://hermes-agent.home.arpa/` and `https://hermes-agent.home.arpa:8443/health`; never use `curl -k`.

Validate the merged configuration and image before restart:

```sh
docker compose config
docker compose run --rm caddy caddy list-modules
docker compose -f docker-compose.yaml run --rm caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d --no-deps caddy
docker exec caddy-hermesagent caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Container validation may be skipped on a developer host without Docker access, but it is a production activation gate. This design uses direct private-PKI HTTPS only—no cloud tunnel, public cloud proxy, or plaintext public route.

### Trust the primary voice server's internal CA on the bridge host

The primary voice server may run a separate Caddy instance and therefore a separate internal CA for `primary-voice-server.home.arpa`. Before enabling HTTPS STT or OmniVoice providers, export only that Caddy authority's `root.crt` from its persistent data directory. Never export its private key. Record the SHA-256 fingerprint on the primary voice server and verify it over a separate trusted channel on the bridge host.

After fingerprint verification, install the certificate into the bridge host's system trust store:

```sh
install -o root -g root -m 0644 primary-voice-server-root.crt \
  /usr/local/share/ca-certificates/talktohermes-primary-voice-server.crt
update-ca-certificates
```

The bridge's HTTPX clients use the system trust store. Verify both `https://primary-voice-server.home.arpa:9443/` and `:9444/ready` without `-k` before activating the bridge. If both Caddy instances intentionally share one private CA, document and fingerprint that common authority instead; do not assume the bridge-facing Caddy root also signs the voice-server certificates.

### Install and trust the internal Root CA on iOS

Caddy stores its local authority under the persistent data volume. After the validated container has created the authority, an administrator exports **only** `/data/caddy/pki/authorities/local/root.crt` from `caddy-hermesagent`. Never copy, publish, or transfer the adjacent private key. Record the certificate SHA-256 fingerprint on the Caddy host and compare it over a separate trusted channel before installation on the device.

Transfer the root certificate directly to the managed iPhone/iPad (for example with an authenticated MDM configuration profile or local AirDrop), not through a public URL, chat, or email. On iOS, install the downloaded profile in **Settings → General → VPN & Device Management**, then explicitly enable the installed root under **Settings → General → About → Certificate Trust Settings**. Verify the displayed subject and fingerprint first. A certificate appearing as installed is not sufficient until full trust is enabled. Finally, open both `https://hermes-agent.home.arpa/` and the configured custom port without a TLS warning and run the authenticated application gate. Never use `curl -k`, disable URLSession trust evaluation, or install the Caddy private key on a client.

## Validation and auth gates

Check listeners and service logs without printing credentials:

```sh
ss -ltnp 'sport = :18081'
systemctl status talktohermes@INSTANCE.service --no-pager
journalctl -u talktohermes@INSTANCE.service -n 100 --no-pager
curl --fail --silent --show-error http://127.0.0.1:18081/health
```

Auth gates are mandatory: no token and a known-wrong token must return `401`; the instance token must return `200` from `/v1/status`. Avoid putting the real token in argv by using a protected temporary curl config:

```sh
curl --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:18081/v1/status
curl --header 'Authorization: Bearer deliberately-wrong' --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:18081/v1/status
umask 077; AUTH_FILE=$(mktemp)
read -r -s -p 'APP_TOKEN: ' TOKEN; printf '\n'
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$AUTH_FILE"; unset TOKEN
curl --config "$AUTH_FILE" --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:18081/v1/status
rm -f "$AUTH_FILE"; unset AUTH_FILE
```

Repeat the authenticated request through `https://hermes-agent.home.arpa:CUSTOM_PORT/v1/status`. Validate certificate hostname and chain; never use `curl -k`.

### Cross-token isolation

For two instances A and B, verify A's protected curl config gets `200` only on A and `401` on B; repeat in reverse. Also confirm distinct YAML ports, secret files, state directories, SQLite files, Hermes roots/profiles, and public HTTPS ports. Never copy a token between instances merely to make this test pass.

Finally run one representative create/voice/status/audio flow through HTTPS. Confirm it creates a session in only that instance's Hermes profile and that the existing Telegram/Hermes gateway still functions unchanged.

## Upgrade

1. Build and fully test a new immutable `REVISION`; retain the previous release.
2. Repeat port ownership checks; the current instance service is the only acceptable bridge owner.
3. Stage `/opt/talktohermes/releases/NEW_REVISION`, install its pinned dependencies, and validate the CLI with a non-listening unit test—do not manually launch a second server.
4. Atomically repoint `current`, then explicitly restart (an already active unit will not pick up a new symlink otherwise):

   ```sh
   ln -sfn /opt/talktohermes/releases/NEW_REVISION /opt/talktohermes/current
   systemctl daemon-reload
   systemctl restart talktohermes@INSTANCE.service
   systemctl is-active talktohermes@INSTANCE.service
   ```

5. Repeat health, auth, HTTPS, representative voice flow, Telegram preservation, and cross-token checks. Upgrade the custom Caddy image only when required, with separately reviewed pins and container validation.

## Rollback

Do not delete the failed release or database before diagnosis. Capture bounded logs, point `current` to the known-good release, and restart:

```sh
journalctl -u talktohermes@INSTANCE.service -n 200 --no-pager
systemctl stop talktohermes@INSTANCE.service
ln -sfn /opt/talktohermes/releases/PREVIOUS_REVISION /opt/talktohermes/current
systemctl daemon-reload
systemctl restart talktohermes@INSTANCE.service
systemctl is-active talktohermes@INSTANCE.service
```

Repeat all validation gates. Roll back Caddy by restoring the previously inspected image digest and complete prior Caddy configuration/Compose files, then run `caddy list-modules` and `caddy validate` before restart. Database rollback is a separate, explicit recovery decision: stop the service first and restore only a verified private backup compatible with the old release.

## Restart and recovery

The unit retries failures at most three times per 60 seconds, five seconds apart, and grants 20 seconds for shutdown. On repeated failure it stays failed instead of looping indefinitely. Diagnose with bounded `journalctl`, correct permissions/config/port ownership, then use `systemctl reset-failed talktohermes@INSTANCE.service` and `systemctl restart ...`. Never start a second manual uvicorn process against the SQLite database.
