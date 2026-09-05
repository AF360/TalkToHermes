# TalkToHermes Security-Baseline

## Isolation pro Instanz

Jeder Bridge-Prozess ist dauerhaft an genau einen Unix-Benutzer und ein Hermes-Home/-Profil gebunden. Jede Instanz besitzt eigene externe HTTPS-Ports, interne Loopback-Ports für Bridge und Hermes API, App-Token und Hermes-API-Key sowie eigene Konfiguration, SQLite-Datenbank, Runtime-/Audio-Verzeichnis und Session-Zuordnung.

Der Client kann weder `profile` noch `instance_id` übermitteln. Instanzfremde Tokens führen zu `401`. Die Wiederverwendung eines State-Verzeichnisses, unsichere Eigentumsverhältnisse oder eine Port-Kollision verhindern den Start.

## Authentifizierung

- Die Hermes API verwendet einen eigenen zufälligen Schlüssel und bleibt auf `127.0.0.1:<Port-pro-Benutzer>`.
- Die iOS-App verwendet einen separaten zufälligen Bearer-Token in der Keychain.
- Host und Port liegen in den App-Einstellungen; der Bearer-Token niemals.
- Der Keychain-Token wird nur für unveränderten normalisierten HTTPS-Host und Port wiederverwendet. Änderungen erfordern explizite Token-Eingabe.
- Authentifizierte Redirects werden nur innerhalb desselben HTTPS-Schemas, Hosts und Ports verfolgt.
- Tokens gelangen niemals in Quellcode, Kommandoargumente, Screenshots, Logs oder Antworten.
- Fehlende, wiederverwendete oder schwache Secrets führen zu Fail-Closed.
- Secret-Dateien besitzen Modus `0600` oder werden als systemd-Credentials bereitgestellt.

## Netzwerk

- Alle Instanzen verwenden `hermes-agent.home.arpa` unter `192.168.100.10` mit unterschiedlichen HTTPS-Ports.
- Die native App enthält keinen einkompilierten installationsspezifischen Endpunkt. Sie akzeptiert zur Laufzeit Hostname und Port, erzeugt ausschließlich HTTPS-URLs und lehnt Schemas, Credentials, eingebettete Ports, Pfade, Queries, Fragmente und ungültige DNS-Labels ab.
- Caddy ordnet jeden externen Port genau einer Loopback-Bridge zu.
- `home.arpa`-TLS verwendet Caddys private PKI (`tls internal`); die interne Root-CA wird per Fingerprint verifiziert und auf verwalteten iOS-Geräten ausdrücklich vertraut. Kein öffentlicher ACME-DNS-Provider und kein DNS-API-Token.
- Öffentliches Routing, Portweiterleitungen, Tunnel-Exposition und öffentliche Webhooks bleiben deaktiviert.
- Direktes IP-TLS ist nur mit einem vertrauenswürdigen Zertifikat zulässig, das die private IP als SAN enthält. Die Prüfung wird niemals umgangen.
- Hermes API, `hermes serve`, `/api/audio/*`, Wyoming, Coglet, OmniVoice und Piper bleiben private Backend-Schnittstellen.
- OmniVoice akzeptiert ausschließlich einen konfigurierten RFC-1918-IPv4-Listener auf Port `9090`. `0.0.0.0`, Loopback-, öffentliche, Dokumentations- oder ungültige Adressen, Hostnamen, Port `8181` und jeder andere Port führen vor Uvicorn zu Fail-Closed. Der Listener-Preflight bindet exakt das validierte Tupel.

## Eingabekontrollen

```text
maximaler Upload: 10 MiB
maximale Dauer: 120 Sekunden
akzeptierte Container: m4a, caf, wav, ogg
```

Die Bridge validiert Medien, ignoriert übermittelte Dateinamen, erzeugt Pfade selbst, startet Subprozesse ohne Shell, begrenzt Ausgabe/stderr und beendet bei Timeout vollständige Prozessbäume.

Der Voice Worker validiert Interpreter, Virtual-Environment-Grenze, Hermes-Root sowie Eigentümer und Modi des Worker-Skripts vor dem Start und schützt sie per Fingerprint gegen Austausch. Interpreter und Worker werden ohne Folgen von Symlinks geöffnet und über geerbte `/proc/self/fd`-Deskriptoren im isolierten Python-Modus mit minimaler Umgebung ausgeführt. OmniVoice-Referenzaudio und Transkripte sind private Serverdateien; gehaltene Deskriptoren verhindern Path-Replacement-Races, und Requests dürfen nur logische Voice-IDs aus einer Allowlist auswählen.

## Tool-Freigaben

MVP-Entscheidungen sind nur `once` (konkrete Aktion einmalig genehmigen) und `deny` (ablehnen). `session` und `always` stehen nicht zur Verfügung. Eine Freigabe bleibt sichtbar, auch wenn Chat-Text ausgeblendet ist. Rein sprachbasierte Freigaben werden abgelehnt. Fehlende oder abgelaufene Freigaben gelten als Ablehnung.

## Datenaufbewahrung

- Fehlgeschlagene diagnostische Uploads werden standardmäßig höchstens 24 Stunden aufbewahrt; `retain_failed_audio=false` entfernt sie sofort.
- Abgeschlossene und abgebrochene Uploads werden sofort entfernt; durch Neustart unterbrochene Arbeiten niemals aufbewahrt.
- Nicht heruntergeladenes Antwort-Audio verfällt nach 24 Stunden. Der erste authentifizierte GET startet ein festes fünfminütiges Retry-/Reconnect-Lease; Wiederholungen verlängern es nicht.
- Transkripte und Antworttext bleiben nach terminalem Turn höchstens 24 Stunden lokal lesbar. Danach werden beide Felder geschwärzt und texttragende `hermes.delta`-Events entfernt; inhaltsfreie Metriken und Provider-Versuchsereignisse bleiben. Aktive Turns werden nicht geschwärzt.
- Bereinigung läuft beim Start und periodisch, ist idempotent, bleibt direkt im Audio-Root und folgt keinen Symlinks bzw. entfernt keine Einträge mit falschem Eigentümer/Modus.
- Die Hermes-Session bleibt der längerlebige kanonische Gesprächskontext. Lokale Textschwärzung und TalkToHermes-Konversationslöschung löschen derzeit nicht die Hermes-Session.
- Textsichtbarkeit ist nur eine Client-Präsentationseinstellung.
- Clone-Referenzen, erzeugtes Audio, Tokens, Datenbanken und private Stimmen bleiben außerhalb von Git.

## Logging

Zulässig sind undurchsichtige Request-/Turn-IDs, Zustände, Laufzeiten, Provider-Namen, Token-Anzahlen und begrenzte Fehlercodes. Verboten sind Credentials/Authorization-Header, Roh-Audio, Clone-Referenzen, standardmäßig Transkripte, vollständige Tool-Payloads/-Ergebnisse, Dateisystempfade an Clients und ungeschwärzte Model-/Provider-Exceptions.

## Deployment-Berechtigungen

Jeder Bridge- und Hermes-API-Prozess läuft als sein eigener unprivilegierter Unix-Benutzer. Gemeinsam genutzte root-eigene Voice-Adapter dürfen nur les-/ausführbar sein. Das Deployment erhält ausschließlich ausdrücklich aufgezählte Befehle: keine allgemeine sudo-Shell, Wildcards, Interpreter-Freigabe, Home-Assistant-Schreibberechtigung oder Remote-Root-Keys.
