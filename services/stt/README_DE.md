# Dediziertes TalkToHermes-STT auf dem primären Voice-Server

Dieses Repository-Artefakt ersetzt weder den Legacy-STT-Listener auf `5005` noch andere Voice-Anwendungen oder globale Hermes-Einstellungen. Der dedizierte Dienst läuft unprivilegiert, lauscht nur auf `127.0.0.1:5050` und wird ausschließlich durch Caddy als `https://primary-voice-server.home.arpa:9444` exponiert.

## Voraussetzungen

Erwartet werden eine verifizierte `faster-whisper`/PyAV-Umgebung in `/opt/stt`, lokaler Modell-Cache, kompatible CPU-/NVIDIA-Runtime und privater Caddy-TLS-Ingress. Keine zweite CUDA-Installation oder mutable Modellauflösung nur für diesen Wrapper.

## Runtime-Vertrag

- Interpreter: `/opt/stt/.venv/bin/python`.
- Modell: gecachter `large-v3-turbo`-Snapshot `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`, per absolutem Pfad, CUDA/float16 und Offline-Flags.
- PyAV 16.0.1 validiert Uploads im Speicher: WAV, FLAC, M4A/MP4 audio-only, MP3, Ogg/Opus, WebM audio-only; genau ein Mono-Audiostream, kein Video, 8–48 kHz, decodierbar, maximal 120 s.
- `GET /ready` und `POST /v1/audio/transcriptions` benötigen Bearer-Token. Readiness lädt das reale Modell. Form: eine `file`, `model=large-v3-turbo`, gültiger Sprach-Tag und fehlendes oder `json`-`response_format`.
- Gunicorn: ein Sync-Worker/ein Thread; Access-Logging aus; keine Credentials, Pfade oder Transkripte in Logs.

## Preflight

```sh
/opt/stt/.venv/bin/python -c 'import av, flask, faster_whisper; print(av.__version__)'
/opt/stt/.venv/bin/python -c 'import av; assert av.__version__ == "16.0.1"'
test -f /opt/stt/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/model.bin
ss -ltnp 'sport = :5050'
ss -ltnp 'sport = :9444'
systemctl --user is-system-running
loginctl show-user VOICE_SERVICE_USER -p Linger
```

5050 muss bei Neuinstallation frei sein; 9444 frei oder vom geprüften Caddy belegt. Port 5005 nicht verändern. Bestehende Caddy-Routen und TalkWithMe prüfen.

## Geprüftes Offline-Gunicorn-Target

`gunicorn.requirements.lock` pinnt Gunicorn 23.0.0. Wheel auf einem Review-/Build-Host mit `--require-hashes --no-deps` herunterladen, SHA-256 prüfen, auf dem Ziel erneut prüfen und ohne Index/Dependency-Auflösung nach `/opt/talktohermes-stt/vendor` installieren. Kein unbeschränktes `pip install`, `--require-hashes` nicht weglassen und Gunicorn nicht ins bestehende STT-Venv installieren.

## Token und Release

Geprüften Code als unveränderliches root-eigenes Release unter `/opt/talktohermes-stt/releases/REVISION` bereitstellen und `current` atomar darauf zeigen lassen. Token geschützt erzeugen:

```sh
install -d -m 0700 "$HOME/.config/talktohermes-stt"
umask 077
read -r -s -p 'Dedicated STT token: ' TOKEN; printf '\n'
printf '%s\n' "$TOKEN" > "$HOME/.config/talktohermes-stt/token"; unset TOKEN
chmod 0600 "$HOME/.config/talktohermes-stt/token"
test ! -L "$HOME/.config/talktohermes-stt/token"
```

Token: 32–256 URL-sichere Zeichen `A-Z a-z 0-9 _ -`, einzigartig für diesen Service.

## User-Unit validieren und aktivieren

```sh
systemd-analyze --user verify ~/.config/systemd/user/talktohermes-stt.service
systemctl --user daemon-reload
systemctl --user enable talktohermes-stt.service
systemctl --user restart talktohermes-stt.service
systemctl --user is-active talktohermes-stt.service
ss -ltnp 'sport = :5050'
journalctl --user -u talktohermes-stt.service -n 100 --no-pager
```

Listener muss exakt `127.0.0.1:5050` mit einem Gunicorn-Worker sein. Netzwerkisolation erfolgt fail-closed über den Loopback-Bind; Caddy ist einziger TLS-Ingress.

## Caddy TLS 9444

`deployment/Caddyfile.stt` in das bestehende Caddyfile integrieren, niemals andere Routen ersetzen. Gesamtkonfiguration validieren und bestätigen, dass 9444 ausschließlich `127.0.0.1:5050` proxyt. Backend-Basis-URL: `https://primary-voice-server.home.arpa:9444`; der Adapter ergänzt `/v1/audio/transcriptions`.

## Abnahme

Unauthentifiziert muss `401`, authentifizierte Readiness `200` liefern. Reale repräsentative Transkription, gültiges Zertifikat/Hostname ohne `-k` und keine Transkripte/Tokens/Pfade im Journal prüfen. Port 5005, TalkWithMe und Hermes/Telegram müssen unverändert funktionieren. Bei Boot-Persistenz Linger und Reboot testen.

## Upgrade und Rollback

Neue unveränderliche Releases bereitstellen/testen, Unit/Caddy erneut validieren, `current` atomar umstellen und den User-Service explizit neu starten. Alle Gates wiederholen. Bei Fehlern begrenztes Journal sichern, `current` auf das vorherige Release zurückstellen, neu starten und Readiness plus reale Inferenz erneut prüfen. Produktion niemals als Ad-hoc-Rollback auf Plaintext-Port 5005 umleiten.
