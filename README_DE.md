# TalkToHermes

![TalkToHermes](images/TalkToHermes.png)

Privates natives Sprach- und optionales Text-Frontend für einen Hermes-Agenten auf iPhone und iPad.

**Mehrsprachig:** `en` / `de` werden bereitgestellt. App-Sprache und gesprochene Sprache können unabhängig voneinander gewählt werden.

## Architektur

```text
iPhone/iPad
  -> hermes-agent.home.arpa:<HTTPS-Port pro Instanz>
  -> schmale TalkToHermes Voice Bridge pro Instanz
  -> offizielle Hermes Sessions/Runs API auf Loopback
  -> geordnete Voice-Provider über begrenzte Adapter
```

Jeder Benutzer besitzt einen separaten Prozess, Token, Port, ein eigenes Hermes-Home, eine Datenbank, einen Audio-Speicher und eine Session-Zuordnung. Die App wählt einen konfigurierten Bridge-Endpunkt, kann diese Bridge jedoch nicht zum Wechsel des Profils oder der Instanz veranlassen.

## Provider-Richtlinie

```text
STT: geordnete Provider pro Instanz (OpenAI-kompatibel -> optional Wyoming -> lokal)
TTS: geordnete Provider pro Instanz (OmniVoice -> optional Wyoming-Piper -> lokales Piper)
```

Die Listenreihenfolge ist die Fallback-Reihenfolge. Wird ein optionaler mittlerer Provider weggelassen, fällt die Verarbeitung direkt auf den konfigurierten lokalen letzten Rückfall zurück. Nur lokales STT/Piper wird als lokal degradierter Betrieb markiert. OmniVoice lauscht auf einer konfigurierten RFC-1918-IPv4-Adresse und dem festen Port `9090`; Wildcard-, Loopback-, öffentliche oder ungültige Adressen sowie jeder andere Port werden beim Laden der Konfiguration und beim Listener-Preflight abgelehnt.

## Status

Bridge und nativer Sprachpfad implementieren begrenzten privaten Upload, STT-Fallback, offizielle Hermes Run/SSE/Approval/Cancel-Funktionen, qualitätsorchestriertes TTS für die vollständige Antwort, authentifizierte Audio-Auslieferung, Wiederherstellung nach Neustart und begrenzte Aufbewahrung. Die Beispiele für dediziertes STT (`9444`) und OmniVoice (`9443`) verwenden authentifiziertes TLS auf `primary-voice-server.home.arpa`. Der SwiftUI-Client bietet Keychain-gestützte Authentifizierung, transaktionale Einstellungen, Wiederholen/Stoppen und Aufzeichnung mit Tap-to-Interrupt. Der authentifizierte Bridge-Status liefert dem Client die konfigurierte Instanz-ID und den sichtbaren Assistentennamen. Ein begrenztes, vom Betreiber konfigurierbares Voice-Overlay bewahrt die ausgewählte Hermes-Identität und den Safety-Prompt, während der Client ausschließlich `short`, `normal` oder `detailed` auswählen kann.

## Maßgebliche Dokumente

- [Architektur](docs/architecture_DE.md)
- [Security-Baseline](docs/security_DE.md)
- [iOS-Client-Einrichtung und Endpunktkonfiguration](ios/README_DE.md)
- [Produktiv-Deployment und Betrieb](deployment/README_DE.md)
- [API-Vertrag](api/openapi.yaml)
- [OmniVoice-Service](services/omnivoice/README_DE.md)
- [STT-Service](services/stt/README_DE.md)
- [Wiederverwendung und Herkunft der Voice-Komponenten](docs/upstream-voice-reuse_DE.md)
- [Lizenz](LICENSE)
