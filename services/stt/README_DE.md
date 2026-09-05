# Dediziertes TalkToHermes-STT auf dem primären Voice-Server

Diese Repository-Komponente stellt den dedizierten TalkToHermes-STT-Endpunkt bereit. Der Dienst läuft unter einem eigenen unprivilegierten Benutzer, lauscht nur auf `127.0.0.1:5050` und wird ausschließlich durch Caddy als `https://primary-voice-server.home.arpa:9444` exponiert. Globale Hermes-Voice-Einstellungen werden nicht verändert.

## Voraussetzungen

Erwartet werden eine bereits geprüfte [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)-/PyAV-Umgebung unter `/opt/stt`, ein lokaler Modell-Cache, eine kompatible CPU- oder NVIDIA-Runtime und privater Caddy-TLS-Ingress. Siehe die zentralen [Deployment-Voraussetzungen](../../deployment/README_DE.md#voraussetzungen). Keine zweite CUDA-Installation oder veränderliche Modellauflösung nur für diesen Wrapper einführen; zuerst die nachfolgend beschriebene Umgebung prüfen.

## Runtime-Vertrag

- Interpreter und installierter ML-Stack: `/opt/stt/.venv/bin/python`.
- Modell: ausschließlich der bereits gecachte `large-v3-turbo`-Snapshot `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` unter `/opt/stt/.cache/huggingface/hub`; Laden über den absoluten lokalen Pfad mit CUDA/float16 und Offline-Umgebungsvariablen. Weder ein veränderlicher Modellname noch Netzauflösung wird verwendet.
- PyAV 16.0.1 validiert hochgeladene Medien vor der Inferenz im Speicher. Erlaubt sind WAV, FLAC, M4A/MP4 nur mit Audio, MP3, Ogg/Opus und WebM nur mit Audio. Genau ein Mono-Audiostream, kein Video, 8–48 kHz, decodierbare Frames und höchstens 120 decodierte Sekunden sind erforderlich. Gezählt werden Samples statt unzuverlässiger Container-Dauermetadaten.
- `GET /ready` und `POST /v1/audio/transcriptions` benötigen den Bearer-Token. Readiness lädt das reale Modell. Das Transkriptionsformular enthält genau eine `file`, `model=large-v3-turbo`, einen validierten Sprach-Tag wie `de`, `en` oder `en-US` und ein fehlendes oder auf `json` gesetztes `response_format`.
- Gunicorn verwendet einen Sync-Worker und einen Thread. Access-Logging ist deaktiviert. Fehler und Logs enthalten keine Credentials, Dateinamen/Pfade, Bodies oder Transkripte.

## Preflight ohne Änderungen

Auf dem primären Voice-Server die vorhandene Umgebung prüfen, statt einen weiteren CUDA-Stack zu installieren:

```sh
/opt/stt/.venv/bin/python -c 'import av, flask, faster_whisper; print(av.__version__)'
/opt/stt/.venv/bin/python -c 'import av; assert av.__version__ == "16.0.1"'
test -f /opt/stt/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/model.bin
ss -ltnp 'sport = :5050'
ss -ltnp 'sport = :9444'
systemctl --user is-system-running
loginctl show-user VOICE_SERVICE_USER -p Linger
```

Port 5050 muss bei einer Neuinstallation frei sein; Port 9444 muss frei oder durch die geprüfte lokale Caddy-Konfiguration belegt sein. Vorher sicherstellen, dass sich das Caddy-Fragment ohne Änderungen an unabhängigen Routen integrieren lässt.

## Geprüftes Offline-Gunicorn-Target erstellen

`gunicorn.requirements.lock` pinnt das universelle Wheel von Gunicorn 23.0.0 auf den von PyPI veröffentlichten SHA-256. PyPI weist das Projekt als MIT-lizenziert aus; das Wheel ist 85.029 Byte groß. Die einzige Runtime-Abhängigkeit `packaging` muss in der konfigurierten STT-Umgebung bereits als Version 25.0 vorhanden sein und wird bewusst nicht überschrieben. Auf einem vernetzten Review-/Build-Host genau dieses Wheel laden:

```sh
mkdir wheelhouse
/opt/stt/.venv/bin/python -m pip download --only-binary=:all: --no-deps \
  --require-hashes --dest wheelhouse -r gunicorn.requirements.lock
sha256sum -c <(printf '%s  %s\n' \
  ec400d38950de4dfd418cff8328b2c8faed0edb0d517d3394e457c317908ca4 \
  wheelhouse/gunicorn-23.0.0-py3-none-any.whl)
```

Nach der Übertragung zum Voice-Server die Prüfsumme erneut kontrollieren und anschließend ohne Paketindex oder Dependency-Auflösung in ein dediziertes root-eigenes Vendor-Target installieren:

```sh
/opt/stt/.venv/bin/python -m pip install --no-index --find-links wheelhouse \
  --require-hashes --no-deps --target /opt/talktohermes-stt/vendor \
  -r gunicorn.requirements.lock
PYTHONPATH=/opt/talktohermes-stt/vendor /opt/stt/.venv/bin/python -m gunicorn --version
/opt/stt/.venv/bin/python -c 'import packaging; assert packaging.__version__ == "25.0"'
```

Kein unbegrenztes `pip install` ausführen, `--require-hashes` nicht weglassen und Gunicorn nicht in die bestehende STT-Virtualenv installieren.

## Token und Release installieren

Geprüften Repository-Code als unveränderliches root-eigenes Release unter `/opt/talktohermes-stt/releases/REVISION` bereitstellen und `/opt/talktohermes-stt/current` atomar darauf zeigen lassen. Der Service schreibt weder Cache- noch Runtime-Dateien.

Token ohne Aufnahme in argv oder Shell-Historie erstellen:

```sh
install -d -m 0700 "$HOME/.config/talktohermes-stt"
umask 077
read -r -s -p 'Dedicated STT token: ' TOKEN; printf '\n'
printf '%s\n' "$TOKEN" > "$HOME/.config/talktohermes-stt/token"; unset TOKEN
chmod 0600 "$HOME/.config/talktohermes-stt/token"
test ! -L "$HOME/.config/talktohermes-stt/token"
test "$(stat -c '%u %a %F' "$HOME/.config/talktohermes-stt/token")" = "$(id -u) 600 regular file"
```

Der Wert muss aus 32–256 URL-sicheren Zeichen (`A-Z`, `a-z`, `0-9`, `_`, `-`) bestehen und für diesen Dienst einzigartig sein.

## User-Unit validieren und aktivieren

`deployment/talktohermes-stt.service` nach `~/.config/systemd/user/` kopieren. Der User-Manager kann weder cgroup-IP-Firewalls installieren noch ein explizites Capability-Set abwerfen; diese Direktiven fehlen daher bewusst. Die Netzgrenze ist am tatsächlichen Listener fail-closed: Gunicorn bindet nur `127.0.0.1:5050`, Caddy ist der einzige TLS-Ingress.

```sh
install -d -m 0700 ~/.config/systemd/user
install -m 0644 deployment/talktohermes-stt.service \
  ~/.config/systemd/user/talktohermes-stt.service
systemd-analyze --user verify ~/.config/systemd/user/talktohermes-stt.service
systemctl --user daemon-reload
systemctl --user enable talktohermes-stt.service
systemctl --user restart talktohermes-stt.service
systemctl --user is-active talktohermes-stt.service
ss -ltnp 'sport = :5050'
journalctl --user -u talktohermes-stt.service -n 100 --no-pager
```

Der Listener muss exakt `127.0.0.1:5050` mit einem Gunicorn-Worker sein. `ProtectHome=read-only`, `ProtectSystem=strict`, `PrivateDevices=false` und `MemoryDenyWriteExecute=false` erlauben Token-/Modellzugriff und NVIDIA/CUDA ohne schreibbare Home- oder Systempfade.

## Caddy TLS 9444 integrieren

`deployment/Caddyfile.stt` in das Caddyfile des primären Voice-Servers integrieren; unabhängige Routen niemals ersetzen. Die vollständige Konfiguration mit dem üblichen `caddy validate` prüfen, Caddy neu laden und bestätigen, dass 9444 ausschließlich nach `127.0.0.1:5050` proxyt. Backend-Basis-URL ist `https://primary-voice-server.home.arpa:9444`; der Adapter ergänzt `/v1/audio/transcriptions`.

## Abnahme

Einen geschützten curl-Config verwenden, damit der Token nicht in argv erscheint:

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

Unauthentifiziert muss `401`, authentifizierte Readiness `200` liefern. Zusätzlich eine reale repräsentative Transkription mit begrenztem JSON-Text, gültiges Zertifikat/Hostname ohne `-k` und ein Journal ohne Transkripte, Tokens oder Pfade verlangen. Unabhängige Caddy-Routen und andere Hermes-Clients müssen weiterhin normal funktionieren. Die Login-Sitzung schließen und den TLS-Endpunkt erneut prüfen; falls Boot-Persistenz erforderlich ist, Linger und einen Reboot separat verifizieren.

## Upgrade und Rollback

Für Upgrades ein neues unveränderliches Release bereitstellen und testen, Unit-/Caddy-Validierung wiederholen, `current` atomar umstellen und `systemctl --user restart talktohermes-stt.service` ausdrücklich ausführen. Alle Abnahme-Gates wiederholen. Bei Fehlern ein begrenztes Journal sichern, `current` auf das vorherige geprüfte Release zurückstellen, neu starten und Readiness plus reale Inferenz erneut prüfen. Den authentifizierten TLS-Endpunkt niemals durch einen nicht authentifizierten Plaintext-Dienst als Ad-hoc-Rollback ersetzen.
