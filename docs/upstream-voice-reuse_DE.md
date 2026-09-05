# Wiederverwendung bestehender Hermes-Voice-Funktionen

Die nachfolgend beschriebenen Implementierungsschnittstellen hängen von den aktuellen Hermes-APIs ab. Vor Änderungen ist die Upstream-Dokumentation erneut zu prüfen.

## Offizielle Dokumentation

- Voice Mode: https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode
- Wake Word: https://hermes-agent.nousresearch.com/docs/user-guide/features/wake-word
- Desktop: https://hermes-agent.nousresearch.com/docs/user-guide/desktop
- Voice & TTS: https://hermes-agent.nousresearch.com/docs/user-guide/features/tts
- API Server: https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server

## Von TalkToHermes genutztes bestehendes Verhalten

| Bedarf | Bestehende Hermes-Schnittstelle | TalkToHermes-Aktion |
|---|---|---|
| Agentenidentität und Tools | offizielle Sessions/Runs API | aufrufen, kein weiteres LLM darum bauen |
| Persistente Konversation | `/api/sessions`, Session-Messages, `/v1/runs` | nur Zuordnung speichern; kanonische Hermes-Historie weiterreichen |
| Streaming-Status | Run-SSE-Events | für Mobilgerät übersetzen/schwärzen |
| Freigaben und Stop | Run-Approval-/Stop-Endpunkte | nur once/deny bereitstellen |
| Bereinigung gesprochenen Texts | `tools.tts_text_normalize.prepare_spoken_text` | über Worker aufrufen |
| Langform-TTS | `tools.tts_tool.text_to_speech_tool` | mit Provider-Override aufrufen |
| OpenAI-kompatibles TTS | `tts.openai.base_url` | mit dediziertem `/audio/speech`-Adapter wiederverwendbar |
| Piper | eingebauter `piper`-Provider plus Wyoming | konfigurierte Remote-, danach lokale Stimme |
| Lokales STT | `transcribe_audio_local_fallback` | über Worker aufrufen |
| Wyoming STT | installierter benannter Kommando-Adapter | konfigurierten Adapter aufrufen |
| GPU STT | `/v1/audio/transcriptions` | dedizierter begrenzter HTTPS-Adapter |
| Desktop-Satzverhalten | `SentenceChunker` und `/api/audio/speak-stream` | Vertragsreferenz, keine Kopie |

## Bewusst nicht exponiert

Hermes Desktop implementiert bereits `POST /api/audio/transcribe`, `POST /api/audio/speak` und `WS /api/audio/speak-stream`. Diese gehören jedoch zur breiten `hermes serve` Desktop-/Dashboard-Oberfläche. Ein Dashboard-Session-Token würde iOS deutlich mehr Berechtigungen geben als für Voice nötig. TalkToHermes nutzt deshalb eine schmale Bridge und einen lokalen Worker und sichert das Verhalten über Contract-Tests.

## Verbleibende eigene Richtlinienlogik

- feste Bridge-Instanz und Token pro Benutzer;
- GPU -> optional Wyoming -> lokaler STT-Failover;
- OmniVoice -> optional Wyoming-Piper mit erlaubter Instanzstimme -> lokaler Piper-Failover;
- OmniVoice-Prüfung auf Auslassungen/Wiederholungen;
- mobiler Upload, Retry/Idempotenz und Aufbewahrung;
- native iOS-Aufzeichnung, Wiedergabe und Approval-UI.

## Verhalten der Hermes-API

`/v1/runs` mit wiederholter `session_id` persistiert Nachrichten, lädt den vorherigen Turn aber nicht automatisch. `/api/sessions/{id}/chat/stream` lädt Historie, stellt jedoch nicht den von der Bridge benötigten Freigabe-Lebenszyklus bereit. TalkToHermes liest deshalb vor jedem Run die kanonischen Hermes-Session-Nachrichten und übergibt sie als `conversation_history`.

## Provider-Herkunft

OmniVoice ist ein unabhängiger Clean-Room-Adapter zur Apache-2.0-API `k2-fsa/OmniVoice`. Der Adapter pinnt `omnivoice==0.2.1` und Modellrevision `c5fdb5ccb189668d56333f77ba2629f4cd7535f4`.

Der lokale Fallback-Review verwendete `piper-tts==1.7.0` von Open Home Foundation (`OHF-Voice/piper1-gpl`) mit offiziellen `rhasspy/piper-voices`-Assets. Geprüfter Wheel-SHA-256: `72adc623b977bdbbdf3d6f6bf88d66eda7cfe2ee8e7919a74a5952acb77a339e`; `pathvalidate==3.3.1` ist MIT-lizenziert, Wheel-SHA-256 `5263baab691f8e1af96092fa5137ee17df5bdfbd6cff1fcac4d6ef4bc2e1735f`. Voice-spezifische Modell-Hashes und Lizenzhinweise sind mit den Deployment-Assets zu prüfen und im Deployment-Nachweis aufzubewahren.

Der dedizierte STT-Service pinnt seinen Offline-`large-v3-turbo`-Snapshot und validiert Medien mit PyAV vor einer serialisierten CUDA/float16-Inferenz. Er exponiert ausschließlich den von TalkToHermes dokumentierten authentifizierten OpenAI-kompatiblen Transkriptionsvertrag.

## Upgrade-Regel

Vor einer neuen Voice-Schnittstelle aktuelle offizielle Dokumentation und Upstream-Main prüfen. Erhält der offizielle API Server stabile Audio-Endpunkte oder Hermes einen geeigneten iOS-Client, soll die entsprechende Eigenkomponente entfernt statt doppelt gepflegt werden.
