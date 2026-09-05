# TalkToHermes-Architektur

![TalkToHermes architecture](/images/TalkToHermes-architecture_DE.png)

## Ziel

TalkToHermes ist ein privates natives Push-to-Talk-Frontend für iPhone/iPad für einen Hermes-Agenten. Es verwendet Identität, Memory, Skills, Tools, Sessions und Freigaben des jeweiligen Betriebssystembenutzers und führt kein separates Persona-Modell ein.

## Wiederverwendung vor Eigenentwicklung

Hermes stellt bereits Sprachaufzeichnung, VAD, satzbewusstes TTS, lokale STT-Wiederherstellung, Piper, OpenAI-kompatibles TTS, Sessions, Runs, SSE, Stop und Freigaben bereit. TalkToHermes ergänzt nur den fehlenden mobilen Client, eine schmale LAN-Grenze, Provider-Richtlinien, Aufbewahrung und OmniVoice-Qualitätsprüfungen. Die breite Desktop-/Dashboard-API wird iOS nicht zugänglich gemacht; insbesondere erhält die App keinen Dashboard-Token und kann `/api/ws` oder `/api/audio/*` nicht direkt aufrufen.

## Komponententopologie

```text
iPhone/iPad
  -> HTTPS: hermes-agent.home.arpa:<Instanz-Port>
  -> Caddy auf 192.168.100.10
  -> eine TalkToHermes Voice-Bridge-Instanz (Loopback)
       -> fester OS-Benutzer/festes Profil und isolierter Zustand
       -> geordnete STT-Provider aus Instanz-YAML
            1. OpenAI-kompatibles GPU-STT: primary-voice-server.home.arpa / 192.168.100.20
            2. optionaler Wyoming-Fallback: fallback-voice-server.home.arpa / 192.168.100.30
            3. konfiguriertes lokales Hermes-Modell
       -> offizieller Hermes API Server auf Loopback-Port pro Benutzer
            -> dedizierte persistente Hermes-Session
       -> geordnete TTS-Provider aus Instanz-YAML
            1. HTTPS OmniVoice auf primary-voice-server.home.arpa
            2. optional Wyoming-Piper auf fallback-voice-server.home.arpa mit warmem dediziertem Port pro Stimme
            3. Hermes Voice Worker mit lokalem Piper und konfigurierter Stimme
  <- gepuffertes Antwort-Audio plus optionaler Text
```

## Multi-Instanz-Modell

Jeder Mensch erhält einen eigenen Prozess, keinen Profilparameter eines gemeinsamen Prozesses. Host plus Port identifizieren die Instanz; Caddy ordnet jeden externen Port statisch genau einer Loopback-Bridge zu. Requests können weder `profile` noch `instance_id` wählen. Jede Instanz hat eigenes Home, externe/Loopback-Ports, App-Token, Hermes-Key, DB, Audio und Session-Mapping.

## Vertrauensgrenzen

1. iOS erreicht nur seine schmale Voice-Bridge-Instanz.
2. Jeder Hermes API Server bleibt Loopback-only und hat einen eigenen Key.
3. App-Token und Hermes-Key sind pro Instanz unterschiedliche Credentials.
4. Wyoming, Coglet, OmniVoice, Piper, Hermes API und `hermes serve` werden nie direkt gegenüber iOS exponiert.
5. Keine Router-Weiterleitung, kein öffentlicher Tunnel, kein Cloud-Proxy.
6. Caddy stellt `home.arpa`-Zertifikate über private PKI (`tls internal`) aus; verwaltete iOS-Clients vertrauen der per Fingerprint geprüften internen Root-CA ausdrücklich.

## Session-Modell

Eine TalkToHermes-Konversation wird einer dedizierten Hermes-Session im konfigurierten Benutzerprofil zugeordnet. Die Zuordnung liegt serverseitig; Hermes-Session-IDs werden nicht exponiert. TalkToHermes-Konversationen werden nicht mit Telegram-Transkripten zusammengeführt. Hermes bleibt der längerlebige kanonische Kontext; das Löschen einer TalkToHermes-Konversation entfernt lokale Zuordnung/Artefakte, derzeit aber nicht die Hermes-Session.

Vor jedem Run liest die Bridge `/api/sessions/{id}/messages` und übergibt diese kanonische Historie als `conversation_history` zusammen mit derselben `session_id` an `/v1/runs`. Dies ist nötig, weil Runs zwar persistieren, vorherigen Session-Kontext derzeit aber nicht automatisch fortsetzen, während Session-Chat-Streaming keine geeignete Run-Approval-Session besitzt. Die Bridge führt keinen zweiten Historien-Speicher.

Jeder Run erhält zusätzlich ein begrenztes, vom Betreiber konfigurierbares `hermes.voice_instructions`-Overlay. Es bewahrt Hermes-Profil, Identität, Memory, Tools und Safety-Prompt und formt nur die gesprochene Ausgabe. Der Client darf ausschließlich `short`, `normal` oder `detailed` wählen; freie System-Prompt-Texte sind nicht möglich. Der authentifizierte Status liefert `instance_id`, aus der der native Client den sichtbaren Assistentennamen ableitet.

## Hermes Voice Worker

Die Bridge kopiert keine Provider-Implementierung. Ein kleiner Subprozess im konfigurierten Hermes-Venv stellt einen begrenzten JSON-stdin/stdout-Vertrag bereit:

```text
tts(piper)  -> text_to_speech_tool(provider="piper")
stt-local   -> transcribe_audio_local_fallback()
normalize   -> prepare_spoken_text()
omnivoice   -> text_to_speech_tool(provider="openai")
               -> separater Wrapper /v1/audio/speech auf primärem Voice-Server
```

Damit bleiben Python-Abhängigkeiten isoliert und Contract-Tests erkennen Upstream-Änderungen. Bestehende Fallback-Voice-Server-Adapter und der OpenAI-kompatible primäre STT-Endpunkt bleiben explizite Bridge-Adapter. OmniVoice nutzt einen separaten TalkToHermes-Service/Port; TalkWithMe auf 8181 bleibt unberührt. Globale Hermes-Voice-Konfiguration wird nicht verändert.

## Verfügbarkeitsmodi

| Modus | STT | TTS |
|---|---|---|
| Normal | erster konfigurierter Provider | erster konfigurierter Provider |
| Primär aus | nächster Provider | nächster Provider |
| Optionaler Fallback fehlt/aus | lokales Modell | lokales Piper, degradierter Modus |

Remote-HTTPS-Provider besitzen getrennte Connect- und Response-Deadlines. Standard: 0,5 s für DNS/TCP/TLS und 120 s für begrenzte Inferenz. Verbindungsfehler öffnen für 45 s einen prozesslokalen Circuit. Währenddessen wird kein Socket geöffnet; danach darf genau ein Half-open-Request passieren. Erfolg schließt den Circuit, erneuter Connectivity-Fehler öffnet ihn wieder. Es gibt weder ICMP-Ping noch Readiness-Preflight.

OmniVoice-Connectivity-Fehler werden nicht erneut versucht, sondern führen direkt zum nächsten TTS-Provider. Tatsächlich synthetisiertes, aber von der Qualitätsprüfung verworfenes WAV erhält genau einen Qualitäts-Retry. Cancellation wird immer propagiert. Attempt-Events enthalten nur logischen Provider, Ergebnis/Fehlercode, Laufzeit, Circuit-State und Fallback – niemals Text, Transkript, Audio, Credentials, Header, URLs oder private Pfade.

Lokales STT und lokaler Piper-Worker teilen einen prozessweiten Concurrency-Slot. Ein Turn meldet `degraded_local_audio=true`, wenn finales STT oder TTS diesen lokalen Pfad nutzte.

## Voice-Turn und Audio-Lebenszyklus

Die authentifizierte Multipart-Route steuert `accepted -> transcribing -> thinking -> awaiting_approval|synthesizing -> completed|failed|cancelled`. Audio plus Sprache, Stimme, Textsichtbarkeit und Antwortstil werden unter der Client-UUID gefingerprintet. Exakte Retries liefern denselben Turn; geänderte Inhalte/Optionen führen zu `409`. Redigierte Events haben atomare Sequenznummern; `Last-Event-ID` spielt nur neuere Events ab. Hermes bleibt kanonischer Gesprächsspeicher.

Uploads werden gestreamt, auf 10 MiB und 120 Sekunden begrenzt, akzeptieren WAV, M4A/MP4, CAF oder Ogg mit validierter Sprache und prüfen Containersignaturen. Dateinamen werden ignoriert. Cancel stoppt den Hermes-Run, beendet/erwartet STT/TTS und bereinigt Artefakte. Startup überführt unterbrochene aktive Turns in einen begrenzten terminalen Fehler und setzt dauerhafte Löschungen fort.

Abgeschlossene/abgebrochene Uploads werden sofort entfernt. Fehlgeschlagene Diagnose-Uploads höchstens 24 h. Antwort-Audio verfällt nach 24 h; erster authentifizierter Download startet ein festes fünfminütiges Reconnect-Lease. Transkripte und Antworttext bleiben nach terminalem Zustand höchstens 24 h und werden dann geschwärzt; texttragende `hermes.delta`-Events werden entfernt. Bereinigung ist idempotent, folgt keinen Symlinks und berührt nur sichere reguläre Dateien direkt im Audio-Root. Eine SQLite-Datenbank hat exklusiv einen Prozess als Eigentümer.

### STT-Vertrag des primären Voice-Servers

`POST /v1/audio/transcriptions` ist ein OpenAI-artiger Multipart-Adapter (`file`, `model`, `language`, `response_format=json`). Er ist eine explizite STT-Stufe und verändert Hermes' globalen STT-Provider nicht. Der Dienst läuft auf Loopback hinter Caddy, mit Bearer-Authentifizierung, 10-MiB-Limit, Medienvalidierung, serialisierter GPU-Inferenz, minimalem Health-Output und Produktions-Supervision.

## TTS-Integrität

Hermes' Normalisierung gesprochenen Texts und Satzverhalten werden wiederverwendet. OmniVoice-Segmente können zurücktranskribiert werden, um Auslassungen, Wiederholungen, leere Ausgabe und unplausible Dauer zu erkennen. Ein qualitätsabgelehntes Segment erhält höchstens einen OmniVoice-Retry; Connectivity-, Timeout-, HTTP-, Format-, Output- und technische Verifier-Fehler wechseln direkt zum nächsten Provider. Wiederholte Qualitätsablehnung erzeugt die gesamte Antwort mit dem nächsten Provider neu, damit die Stimme nicht mitten in der Antwort wechselt.

## Zurückgestellte Fähigkeiten

Wake Word/Always-on-Mikrofon, Streaming-Audio, öffentlicher Internetzugriff, TestFlight/App Store, gemeinsamer Multi-User-Prozess, `session`-/`always`-Freigaben und Hermes-Core-Änderungen.

## Verhalten des nativen Clients und Build-Grenze

Der iOS-17+-SwiftUI-Client akzeptiert Bridge-Hostname, HTTPS-Port und Bearer-Token zur Laufzeit; kein installationsspezifischer Endpunkt wird einkompiliert. HTTPS ist fest. Hostname und Port werden streng normalisiert/validiert; Schemas, Credentials, eingebettete Ports, Pfade, Queries und Fragmente werden abgelehnt. Vor dem Speichern wird `GET /v1/status` authentifiziert geprüft.

Der Bearer-Token liegt mit `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` in der Keychain. Ein leeres Token-Feld darf nur bei unverändertem Host/Port wiederverwenden; Änderungen verlangen expliziten Token. Fehlgeschlagene Validierung lässt alte Einstellungen intakt. Redirects bleiben auf demselben HTTPS-Origin. Endpunktwechsel baut den API-Client neu und löscht die aktuelle Konversationsbindung. Normale URLSession-Zertifikatsprüfung ist zwingend.

Voice-Uploads tragen einen expliziten Sprach-Tag. Die App speichert `de` oder `en` unabhängig von UI-/Gerätesprache; fehlende/ungültige Legacy-Werte fallen auf Deutsch zurück. Aufnahme erfolgt Tap-to-start/Tap-to-send; Wiedergabe kann gestoppt/wiederholt werden; Mikrofon während Wiedergabe stoppt Audio und startet eine neue Aufnahme ohne Kontextverlust. Details: [`ios/README_DE.md`](../ios/README_DE.md).
