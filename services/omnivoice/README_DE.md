# TalkToHermes OmniVoice-Service

Diese unabhängig versionierte Service-Komponente bleibt bei `1.0.0`; Repository und Voice Bridge dürfen weiter versioniert werden, solange Code und Vertrag dieses Adapters unverändert bleiben.

Clean-Room-Implementierung eines dedizierten OpenAI-kompatiblen REST-Adapters für die öffentliche Apache-2.0-Python-API von [k2-fsa/OmniVoice](https://github.com/k2-fsa/OmniVoice). Kein Quellcode aus `scorbo2/ai-playground` wird kopiert oder angepasst; TalkToHermes hängt nicht davon ab.

## Voraussetzungen und Installationsreihenfolge

Accelerator/PyTorch-Runtime, gelockte OmniVoice-Umgebung, gepinnter Modell-Cache, private Referenz-Assets und privater TLS-Ingress müssen vorher bereitstehen. Siehe zentrale Deployment-Voraussetzungen.

1. Accelerator prüfen und passenden PyTorch-Build mit dem offiziellen Selector installieren.
2. Saubere Umgebung gemäß `pyproject.toml` erstellen und `uv sync --frozen` ausführen; keine zweite unbeschränkte Installation ergänzen.
3. Gepinnte `k2-fsa/OmniVoice`-Modellrevision vorab cachen und private WAV/Transkript-Paare anlegen; z. B. Sprache `de` oder `en`.
4. Reales Laden und Synthese mit Ziel-Accelerator und Assets prüfen. Für MP3 `/usr/bin/ffmpeg` installieren.

## Vertrag

- `GET /ready`: Bearer-authentifizierte minimale Readiness.
- `POST /v1/audio/speech`: Bearer-authentifiziertes JSON mit exakt `model`, `voice`, `input`, `response_format`; liefert WAV oder MP3 binär.
- `model=omnivoice`; `response_format=wav|mp3`.
- Listener muss RFC-1918, Nicht-Loopback-IPv4 und Port `9090` sein; Beispiel `192.168.100.20:9090`.
- Voice-IDs sind logische Allowlist-IDs; Referenz-WAV-Pfade/Transkripte existieren nur serverseitig.

OpenAPI und interaktive Doku sind deaktiviert. Inferenz ist serialisiert; parallele Synthese scheitert schnell. Seed `314`, Steps `12`, Guidance `1.2`, Modell und Sprache werden serverseitig kontrolliert. MP3-Konvertierung verwendet nur den festen `/usr/bin/ffmpeg`-Argumentvektor, Pipes, 30-s-Timeout und keine temporären Dateien.

## Konfiguration und Berechtigungen

`config.example.yaml` außerhalb des Repositories kopieren und Platzhalter ersetzen. Der Service lehnt Wildcard-, Loopback-, öffentliche, Dokumentations- und ungültige IPs, Hostnamen, Port `8181`, alle Ports außer `9090`, unbekannte Keys, unsichere Voice-IDs, nicht-private Voice-Dateien, Symlinks, falsche Eigentümer, fehlerhafte WAVs und Token-Dateien ungleich `0600` ab. Assets werden mit `O_NOFOLLOW` geöffnet, per Inode geprüft und über gehaltene Deskriptoren gelesen; Parent-Pfade müssen root-/service-owned und für andere nicht schreibbar sein. Referenz-Assets unter `/var/lib/talktohermes-omnivoice` halten. Token: eigenständiger ASCII-Wert, 32–128 Nicht-Whitespace-Zeichen, optional abschließender Zeilenumbruch.

```sh
sudo install -d -o talktohermes-omnivoice -g talktohermes-omnivoice -m 0700 \
  /etc/talktohermes-omnivoice /var/lib/talktohermes-omnivoice
sudo install -o talktohermes-omnivoice -g talktohermes-omnivoice -m 0600 TOKEN_FILE \
  /etc/talktohermes-omnivoice/token
```

Projekt nach `/opt/talktohermes-omnivoice/venv` installieren, Unit installieren, `systemd-analyze verify` ausführen und aktivieren. `omnivoice==0.2.1` und Dependency-Graph sind in `uv.lock` gepinnt; Modellrevision `c5fdb5ccb189668d56333f77ba2629f4cd7535f4`. Reale CUDA/Torch/Modell-Importe und Synthese nach Änderungen erneut prüfen. Lazy Imports erlauben lokale Tests ohne GPU/Modelldownload.

## Verifiziertes Target-Gate

Ein unprivilegierter Target-Host-Test lief mit `omnivoice==0.2.1` und der gepinnten Modellrevision offline über CUDA und private Referenzdaten erfolgreich. Authentifizierte Readiness lieferte `200 {"status":"ready"}`, Synthese ein gültiges PCM-16-Bit-Mono-24-kHz-WAV. Ein separater STT-Lauf rekonstruierte die deutschen Wörter exakt; Hörprüfung akzeptierte das Ergebnis. Der bestehende Dienst auf Port `8181` blieb gesund. Temporärer Prozess wurde gestoppt, Listener freigegeben und private Artefakte blieben außerhalb des Repositories.

Kein transaktionaler Installer wird ausgeliefert, da Dependency-/Modell-/GPU-Provisionierung auf dem lokalen Nicht-Produktionshost nicht vollständig geprüft werden kann.

## Tests

```sh
pytest -q
```
