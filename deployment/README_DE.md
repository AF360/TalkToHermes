# Produktiv-Deployment (Debian 13 / systemd 257)

Dieses Verzeichnis ist ein vom Betreiber zu prüfendes Beispiel, kein Installer. Es nimmt selbst keine Produktivänderungen vor. Jede Instanz besteht aus einem vorhandenen Unix-Konto, einem Loopback-Bridge-Port, zwei Bridge-Credentials plus Tokens konfigurierter Remote-Provider und einem SQLite-State-Verzeichnis. Niemals zwei Bridge-Prozesse gegen dasselbe State-Verzeichnis betreiben.

Die Bridge bleibt ein systemd-Service; Caddy bleibt das bestehende Docker-Deployment. Das Beispiel installiert bewusst kein systemd-Caddy, ersetzt kein vollständiges Caddyfile und fügt weder Cloud-Tunnel noch öffentlichen Proxy hinzu.

## Voraussetzungen

Dieses Repository bootstrapped keinen Host. Betriebssystem-, Hermes-, Voice-Runtime-, Modell- und TLS-Komponenten vor den Deployment-Skripten bereitstellen und prüfen. Upstream-Beispiele sind keine Projekt-Pins; Lockfiles und service-spezifische Runtime-Verträge haben Vorrang.

### Bridge-Host

Benötigt werden Debian mit systemd-User-Services, ein dediziertes Unix-Konto pro Instanz, `systemctl --user`, `systemd-analyze`, bei Bedarf Linger sowie `git`, `curl`, `ca-certificates`, `coreutils`, `iproute2` und eine geprüfte `uv`-Installation. Bridge-Abhängigkeiten aus `backend/uv.lock` mit `uv sync --frozen` installieren. Hermes Agent muss je Instanz bereits installiert und auf Loopback gesund sein. TalkToHermes installiert/ersetzt weder Hermes noch Telegram-Gateway. Zusätzlich: geprüfter Checkout, private Konfiguration/Secrets, State-Verzeichnis `0700` und private Caddy-PKI.

### Voice-Provider

Nur Provider installieren, die in den geordneten `stt`-/`tts`-Listen vorkommen. Erster Eintrag ist bevorzugt, spätere sind Fallbacks. Für OmniVoice Accelerator/PyTorch, gelockte Umgebung, gepinntes Modell und private Referenzdaten vorab prüfen. Für Faster-Whisper PyAV, Modell-Cache und kompatible CUDA/CTranslate2-Runtime verifizieren. Piper benötigt pro Stimme `.onnx` und `.onnx.json`. Wyoming-TCP ist unverschlüsselt und darf nur auf privaten, per Firewall beschränkten Interfaces laufen.

Keine zweite CUDA-Installation über eine funktionierende geprüfte Umgebung legen. `ffmpeg` dort installieren, wo OmniVoice-MP3, MLX-Whisper oder Wyoming-STT es benötigt. `home.arpa` verwendet Caddy `tls internal`; Root-CA-Fingerprint prüfen und CA auf Bridge/Clients vertrauen. Nie CA-Private-Key übertragen oder `curl -k` verwenden. Modelle, Voice-Dateien, Caches, Tokens und Referenzaudio bleiben außerhalb unveränderlicher Releases.

## Deployment-Skripte

- `deployment/scripts/deploy-hermes-agent-user.sh INSTANCE [REVISION]`: als unprivilegierter Bridge-User; erstellt unveränderliches Git-Archiv-Release, installiert User-Unit, startet Bridge neu und prüft Loopback-Health.
- `deployment/scripts/deploy-primary-voice-server-user-services.sh [REPOSITORY_ROOT] [VOICE_HOST_IP] [REVISION]`: deployt STT und OmniVoice aus einem Commit auf dem GPU-Host und prüft beide Health-Endpunkte.
- `deployment/scripts/deploy-fallback-piper-user.sh INSTANCE VOICE PORT BIND_IP [SERVER_ROOT]`: installiert auf macOS launchd-Agent und warmen Piper-Supervisor, validiert Modell/Port, wärmt Synthese auf und rollt bei Fehler zurück.

Legacy-Root-Daemons werden nicht automatisch deaktiviert; deren Stilllegung ist ein getrennt geprüfter privilegierter Schritt.

## Bevorzugte Bridge-User-Service-Installation

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

Alle `INSTANCE`-Literale ersetzen, Provider/Loopback-Port setzen und unabhängige Secrets in die `0600`-Datei eintragen. Keine Credentials in YAML oder argv.

```sh
deployment/scripts/deploy-hermes-agent-user.sh "$INSTANCE" REVISION
systemctl --user is-enabled "talktohermes@$INSTANCE.service"
systemctl --user is-active "talktohermes@$INSTANCE.service"
```

Für Start ohne Login einmalig `loginctl enable-linger INSTANCE`. Die User-Unit verzichtet bewusst auf problematische Mount-Namespace-/cgroup-IP-Firewall-Direktiven eines unprivilegierten User-Managers; die übrigen Einschränkungen bleiben aktiv. Legacy-System-Service-Pfade unter `/opt`, `/etc`, `/var/lib` nicht mit dem User-Service-Layout mischen.

## Invarianten und Annahmen

Instanzname ist ein bestehendes dediziertes Unix-Konto und gültige `instance_id`. Legacy-Releases und `current` sind root-owned. YAML enthält keine Credentials; Secret-Dateien sind `0600`; State-Verzeichnis ist `0700` und wird nie geteilt. Hermes bleibt auf `127.0.0.1:8642`, die Bridge ebenfalls auf Loopback. `hermes.voice_instructions` ist begrenzt, nicht geheim und sprachbezogen; Clients dürfen nur `short`, `normal`, `detailed` wählen.

Provider-Reihenfolge kommt direkt aus `stt`/`tts`. Remote-HTTPS-Provider besitzen getrennte Connect-, Response- und Circuit-Cooldown-Werte (Standard 0,5 s / 120 s / 45 s). Die Bridge nutzt echte TCP/TLS-Requests statt ICMP-Ping und überspringt während Cooldown nicht erreichbare Endpunkte; danach wird genau ein Half-open-Versuch zugelassen. Doppelte Remote-Endpunkte sind verboten. `text_retention_hours` ist maximal 24; Bereinigung wird deadline-orientiert geplant und bei transienten Fehlern erneut versucht.

Dokumentations-LAN: `192.168.100.10` Hermes, `.20` primärer Voice-Server, optional `.30` Fallback. Diese Werte sind nur Beispiele. Ein öffentlicher HTTPS-Port wird genau einem Loopback-Bridge-Port zugeordnet; kein HTTP-Listener.

## Minimale privilegierte Befehle

Nur Administratoren führen Benutzeranlage, `install/chown/chmod` unter `/opt`/`/etc`/`/var/lib`, Symlink-Umschaltung, systemd-Systemmanager-Kommandos, Docker-Caddy-Operationen und ggf. Firewall-Änderungen aus. Der normale Operator hat weder passwortloses sudo noch Docker-Gruppenrechte; nicht umgehen.

## Port-Kollisions-Preflight

```sh
ss -ltnp 'sport = :18081'
ss -ltnp 'sport = :8443'
```

Bei Neuinstallation müssen beide frei sein. Beim Upgrade darf nur der bekannte TalkToHermes-Service den Bridge-Port besitzen. Außerdem Abgrenzung zu Hermes `8642`, Caddy `80/443` und Caddy-Admin `2019` prüfen.

## Legacy-Root/System-Service-Installation

Geprüfte Revision zunächst unprivilegiert testen (`pytest`, `uv lock --check`). Danach als Administrator ein unveränderliches Git-Archiv unter `/opt/talktohermes/releases/REVISION` mit expliziter sicherer tar-umask bereitstellen, `uv sync --frozen --no-dev --no-editable` ausführen, Eigentümer root setzen und `current` atomar umschalten. Keine Entwickler-`.venv` kopieren und keine unbeschränkte Dependency-Auflösung verwenden.

Konfiguration nach `/etc/talktohermes`, State nach `/var/lib/talktohermes/INSTANCE`. Secret-Datei aus leerer `0600`-Datei erzeugen, Tokens per stillem `read` erfassen und Shell-Variablen sofort löschen. Alle Tokens müssen unabhängig, 32–256 Zeichen lang und auf Buchstaben/Ziffern/`_`/`-` beschränkt sein.

### Optionaler Wyoming-STT-Adapter

Nur bei `type: wyoming` erforderlich. Wrapper und exakt geprüfte `wyoming`-Version in dedizierter Umgebung bereitstellen; URI kommt über `--uri`, kein privater Host wird einkompiliert. Ohne Wyoming-Eintrag wird der Pfad weder validiert noch aufgerufen.

Die System-Unit bleibt standardmäßig fail-closed (`IPAddressDeny=any` plus Loopback). Site-lokale DNS-/Provider-IP-Adressen müssen in einem geprüften Instanz-Drop-in explizit erlaubt werden. Platzhalter, fehlende oder zusätzliche Adressen, fehlgeschlagene DNS-Auflösung oder Provider-Erreichbarkeit blockieren die Aktivierung.

## Bestehende Docker-Caddy-Integration

Produktionsroot ist `/opt/caddy`, Host-Networking sowie persistente `/data`-/`/config`-Volumes bleiben erhalten. Bestehenden Container-/Image-Namen nicht im Rahmen dieses Deployments umbenennen. Standard-HTTPS proxyt nur zum authentifizierten Dashboard; die private Hermes API auf `127.0.0.1:8642` wird nie exponiert. TalkToHermes erhält eine separate Route, z. B. `8443 -> 127.0.0.1:18081`.

Compose und Caddyfile immer zusammenführen, niemals blind ersetzen. Bestehende Hermes-Routen und Storage beibehalten. `home.arpa` nutzt `tls internal` und benötigt keinen DNS-Provider-Secret-Mount. Vollständige Konfiguration vor Neustart mit Caddy validieren und danach beide Routen ohne `curl -k` prüfen.

### Interne CA des primären Voice-Servers auf dem Bridge-Host vertrauen

Nur `root.crt` aus dem persistenten Caddy-Datenverzeichnis exportieren, niemals den privaten Schlüssel. SHA-256-Fingerprint auf dem Voice-Server erfassen und über getrennten vertrauenswürdigen Kanal auf der Bridge vergleichen. Danach als lokale CA installieren und `update-ca-certificates` ausführen. HTTPS auf `:9443` und `:9444/ready` ohne `-k` prüfen.

### Interne Root-CA unter iOS installieren und vertrauen

Nur `/data/caddy/pki/authorities/local/root.crt` exportieren. Fingerprint getrennt prüfen. Zertifikat direkt per vertrauenswürdigem MDM oder lokalem AirDrop auf das verwaltete Gerät übertragen, nicht über öffentliche URL, Chat oder E-Mail. Profil unter **Einstellungen → Allgemein → VPN & Geräteverwaltung** installieren und anschließend unter **Einstellungen → Allgemein → Info → Zertifikatsvertrauenseinstellungen** vollständiges Vertrauen aktivieren. Subjekt/Fingerprint prüfen. Private CA-Schlüssel niemals auf Clients installieren und URLSession-Trust nie deaktivieren.

## Validierungs- und Auth-Gates

Listener, Service-Status und begrenzte Journals prüfen. Ohne Token und mit falschem Token muss `/v1/status` `401`, mit Instanz-Token `200` liefern. Reale Tokens nicht in argv setzen, sondern geschützte temporäre curl-Konfiguration verwenden. Den authentifizierten Request anschließend über den öffentlichen privaten HTTPS-Port wiederholen und Zertifikatskette/Hostname ohne `-k` prüfen.

### Cross-Token-Isolation

Bei zwei Instanzen muss Token A nur auf A `200` und auf B `401` liefern, umgekehrt ebenso. YAML-Ports, Secret-Dateien, State-Verzeichnisse, SQLite-Dateien, Hermes-Roots/Profile und öffentliche HTTPS-Ports müssen verschieden sein. Abschließend repräsentativen Create/Voice/Status/Audio-Flow über HTTPS ausführen und prüfen, dass nur das richtige Hermes-Profil eine Session erhält und Telegram/Hermes unverändert funktioniert.

## Upgrade

Neues unveränderliches Release vollständig testen, Port-Eigentümer erneut prüfen, Release bereitstellen und `current` atomar umschalten. Einen bereits aktiven Service ausdrücklich neu starten. Danach Health, Auth, HTTPS, Voice-Flow, Telegram-Erhalt und Cross-Token-Prüfungen wiederholen. Caddy-Image nur bei Bedarf und mit getrennt geprüften Pins aktualisieren.

## Rollback

Fehlgeschlagenes Release oder Datenbank vor Diagnose nicht löschen. Begrenzte Logs sichern, Service stoppen, `current` auf das bekannte gute Release zurücksetzen, daemon-reload/restart ausführen und alle Gates wiederholen. Caddy auf zuvor geprüften Image-Digest und vollständige alte Konfiguration zurücksetzen und vor Neustart validieren. Datenbank-Rollback ist eine separate explizite Recovery-Entscheidung und darf nur bei gestopptem Service aus einem verifizierten kompatiblen privaten Backup erfolgen.

## Neustart und Recovery

Die Unit versucht Fehler höchstens dreimal pro 60 Sekunden im Abstand von fünf Sekunden erneut und gewährt 20 Sekunden für Shutdown. Bei wiederholtem Fehler bleibt sie failed statt endlos zu loopen. Mit begrenztem `journalctl` diagnostizieren, Berechtigungen/Konfiguration/Port-Eigentum korrigieren und anschließend `systemctl reset-failed` plus Neustart verwenden. Niemals einen zweiten manuellen Uvicorn-Prozess gegen dieselbe SQLite-Datenbank starten.
