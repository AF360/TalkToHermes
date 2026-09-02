# Dedicated primary voice server TalkToHermes STT

This is a repository-only deployment artifact. It does not replace or modify a legacy STT listener on port `5005`, another voice application, or any existing global Hermes voice setting. The dedicated service runs as a dedicated unprivileged service user, listens only on `127.0.0.1:5050`, and is exposed solely by Caddy as `https://primary-voice-server.home.arpa:9444`.

## Prerequisites

This wrapper expects an already verified [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)/PyAV environment in `/opt/stt`, a local model cache, a compatible CPU or NVIDIA runtime, and Caddy private-TLS ingress. See the central [deployment prerequisites](../../deployment/README.md#prerequisites). Do not install a second CUDA stack or resolve a mutable model merely to satisfy the wrapper; first verify the environment described below.

## Runtime contract

- Interpreter and installed ML stack: `/opt/stt/.venv/bin/python`.
- Model: exactly the already-cached `large-v3-turbo` snapshot `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` below `/opt/stt/.cache/huggingface/hub`, loaded by its absolute local path with CUDA/float16 and offline environment flags. No mutable model name or network resolution is used.
- PyAV 16.0.1 validates uploaded media in memory before inference. Accepted extension/MIME pairs are WAV, FLAC, M4A/MP4 audio-only, MP3, Ogg/Opus, and WebM audio-only. Validation requires exactly one mono audio stream, no video, 8–48 kHz, decodable frames, and at most 120 decoded seconds. It counts decoded samples and stops at the cap instead of trusting container duration metadata.
- Both `GET /ready` and `POST /v1/audio/transcriptions` require the bearer token. Readiness loads the real model. The transcription form is exactly one `file`, `model=large-v3-turbo`, a validated language tag such as `de`, `en` or `en-US`, and absent or `json` `response_format`.
- Gunicorn is one sync worker and one thread. Access logging is disabled. Application errors and logs contain no credential, filename/path, body, or transcript.

## Preflight (no changes)

On the primary voice server, verify the existing environment rather than installing another CUDA stack:

```sh
/opt/stt/.venv/bin/python -c 'import av, flask, faster_whisper; print(av.__version__)'
/opt/stt/.venv/bin/python -c 'import av; assert av.__version__ == "16.0.1"'
test -f /opt/stt/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/model.bin
ss -ltnp 'sport = :5050'
ss -ltnp 'sport = :9444'
systemctl --user is-system-running
loginctl show-user VOICE_SERVICE_USER -p Linger
```

Port 5050 must be unused for a new installation. Port 9444 must be unused or owned by the reviewed local Caddy configuration. Do not stop or edit port 5005. Confirm Caddy's existing routes and TalkWithMe before proceeding.

## Build the reviewed offline Gunicorn vendor target

`gunicorn.requirements.lock` pins Gunicorn 23.0.0's universal wheel to the SHA-256 published by PyPI. PyPI identifies the project as MIT-licensed and the wheel is 85,029 bytes. Gunicorn's only runtime dependency, `packaging`, is already present as version 25.0 in the exact Coglet virtualenv and is deliberately not overlaid. Download the one reviewed wheel on a networked review/build host:

```sh
mkdir wheelhouse
/opt/stt/.venv/bin/python -m pip download --only-binary=:all: --no-deps \
  --require-hashes --dest wheelhouse -r gunicorn.requirements.lock
sha256sum -c <(printf '%s  %s\n' \
  ec400d38950de4dfd418cff8328b2c8faed0edb0d517d3394e457c317908ca4 \
  wheelhouse/gunicorn-23.0.0-py3-none-any.whl)
```

After transfer to the primary voice server, verify the checksum again, then install without a package index or dependency resolution into a dedicated root-owned vendor target:

```sh
/opt/stt/.venv/bin/python -m pip install --no-index --find-links wheelhouse \
  --require-hashes --no-deps --target /opt/talktohermes-stt/vendor \
  -r gunicorn.requirements.lock
PYTHONPATH=/opt/talktohermes-stt/vendor /opt/stt/.venv/bin/python -m gunicorn --version
/opt/stt/.venv/bin/python -c 'import packaging; assert packaging.__version__ == "25.0"'
```

Do not run an unbounded `pip install`, omit `--require-hashes`, or install Gunicorn into the existing STT virtualenv.

## Install token and release

Stage reviewed repository code as an immutable root-owned release under `/opt/talktohermes-stt/releases/REVISION`, then atomically point `/opt/talktohermes-stt/current` at it. The service writes no cache or runtime files.

Create the token without placing it in argv or shell history:

```sh
install -d -m 0700 "$HOME/.config/talktohermes-stt"
umask 077
read -r -s -p 'Dedicated STT token: ' TOKEN; printf '\n'
printf '%s\n' "$TOKEN" > "$HOME/.config/talktohermes-stt/token"; unset TOKEN
chmod 0600 "$HOME/.config/talktohermes-stt/token"
test ! -L "$HOME/.config/talktohermes-stt/token"
test "$(stat -c '%u %a %F' "$HOME/.config/talktohermes-stt/token")" = "$(id -u) 600 regular file"
```

The value must be 32–256 URL-safe characters (`A-Z`, `a-z`, `0-9`, `_`, `-`) and unique to this service.

## Validate and activate the user unit

Copy `deployment/talktohermes-stt.service` to `~/.config/systemd/user/`. The primary voice server's user manager cannot install cgroup IP firewalls or drop an explicit capability set; those directives fail or are ignored and therefore are deliberately absent. Network isolation is fail-closed at the actual listener: Gunicorn binds only `127.0.0.1:5050`, while Caddy is the sole TLS ingress.

```sh
systemd-analyze --user verify ~/.config/systemd/user/talktohermes-stt.service
systemctl --user daemon-reload
systemctl --user enable talktohermes-stt.service
systemctl --user restart talktohermes-stt.service
systemctl --user is-active talktohermes-stt.service
ss -ltnp 'sport = :5050'
journalctl --user -u talktohermes-stt.service -n 100 --no-pager
```

The listener must be exactly `127.0.0.1:5050`, with one Gunicorn worker. `ProtectHome=read-only`, `ProtectSystem=strict`, `PrivateDevices=false`, and `MemoryDenyWriteExecute=false` preserve token/model reads and NVIDIA/CUDA operation without writable home/system paths.

## Merge Caddy TLS 9444

Merge `deployment/Caddyfile.stt` into the primary voice server's existing Caddyfile; never replace unrelated routes. Validate the complete merged configuration with the Caddy installation's normal `caddy validate` command, reload Caddy, and confirm that 9444 proxies only to `127.0.0.1:5050`. The backend contract is the base URL `https://primary-voice-server.home.arpa:9444`; the adapter appends `/v1/audio/transcriptions`.

## Acceptance gates

Use a protected curl config so the token is not in argv:

```sh
umask 077; AUTH=$(mktemp)
read -r -s -p 'Dedicated STT token: ' TOKEN; printf '\n'
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$AUTH"; unset TOKEN
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5050/ready
curl --config "$AUTH" --fail --silent http://127.0.0.1:5050/ready
curl --config "$AUTH" --fail --silent https://primary-voice-server.home.arpa:9444/ready
curl --config "$AUTH" --fail --silent \
  -F file=@reviewed-mono.wav -F model=large-v3-turbo -F language=de -F response_format=json \
  https://primary-voice-server.home.arpa:9444/v1/audio/transcriptions
rm -f "$AUTH"; unset AUTH
```

Require unauthenticated `401`, authenticated readiness `200`, a real representative transcription with bounded JSON text, valid certificate/hostname without `-k`, and no transcript/token/path in the journal. Re-check port 5005, TalkWithMe, and existing Hermes/Telegram operation unchanged. Close the login session and verify the TLS endpoint again; if boot persistence is required, separately verify user linger and perform a reboot acceptance test.

## Upgrade and rollback

For upgrades, stage and test a new immutable release, re-run unit/Caddy validation, atomically repoint `current`, and explicitly `systemctl --user restart talktohermes-stt.service`. Repeat all acceptance gates. On failure, capture a bounded journal, repoint `current` to the previous reviewed release, restart, and repeat readiness plus real inference. Never redirect production TalkToHermes to plaintext 5005 as an ad-hoc rollback; 5005 remains separate fallback infrastructure unrelated to this bridge.
