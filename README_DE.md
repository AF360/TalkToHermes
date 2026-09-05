# TalkToHermes

![TalkToHermes](images/TalkToHermes.png)

## Was ist TalkToHermes?

TalkToHermes ist ein privater, nativer Sprachclient für iPhone und iPad, mit dem du natürlich mit deinem eigenen Hermes Agent sprechen und den vollständigen Gesprächsverlauf auf dem Bildschirm verfolgen kannst. Er kombiniert eine sichere Bridge-Isolation pro Benutzer mit konfigurierbaren Fallbacks für Spracherkennung und Sprachsynthese, Freigabe- und Abbruchabläufen in Echtzeit sowie im Schlüsselbund (Keychain) geschützten Zugangsdaten. Die Benutzeroberfläche ist auf Englisch und Deutsch verfügbar; App-Sprache und gesprochene Sprache lassen sich unabhängig voneinander auswählen.

Die zentrale Architekturentscheidung lautet: **lokaler Betrieb für maximalen Datenschutz**. Spracherkennung, unterschiedliche Sprachsynthesen sowie die gesamte Orchestrierung und Steuerung können vollständig auf eigenen Systemen im privaten Netzwerk ausgeführt werden. Die Voice-Provider lassen sich in mehreren Qualitäts- und Fallbackstufen anordnen. Fällt ein bevorzugter Dienst aus, wechselt TalkToHermes automatisch zum nächsten konfigurierten Provider und schließlich zu lokalem Last-Resort-STT beziehungsweise -TTS. Audioaufnahmen und Transkripte müssen dabei die eigene Infrastruktur nicht verlassen. Für einen vollständig lokalen Ende-zu-Ende-Betrieb muss auch Hermes einen lokal betriebenen LLM-Provider verwenden, beispielsweise ein über Ollama bereitgestelltes Modell.

TalkToHermes ist kein eigenständiges Komplettpaket. Neben der iOS-App werden ein installierter und konfigurierter Hermes Agent, die TalkToHermes Voice Bridge sowie ein privater HTTPS-Endpunkt benötigt. Welche Voice-Komponenten zusätzlich erforderlich sind, hängt von der gewählten Provider-Kette ab:

- **Spracherkennung:** Faster-Whisper auf einem geeigneten GPU-System als leistungsfähige primäre STT-Stufe; optional Wyoming-Faster-Whisper als privater Netzwerkdienst sowie MLX-Whisper auf einem Apple-Silicon-Mac oder ein anderes lokales Hermes-STT-Modell als Fallback.
- **Sprachsynthese:** lokales Piper als Last-Resort-TTS; optional ein dauerhaft laufender Wyoming-Piper-Dienst – beispielsweise auf einem Mac – für geringere Latenz und zusätzliche Qualitätsstufen.
- **Geklonte Stimmen:** optional OmniVoice mit einer geeigneten Accelerator-/PyTorch-Umgebung und privaten Referenzaufnahmen.
- **Vollständig lokales Sprachmodell:** ein lokaler Hermes Model Provider, beispielsweise Ollama, wenn auch die Verarbeitung durch das LLM die eigene Infrastruktur nicht verlassen soll.

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
