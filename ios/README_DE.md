# TalkToHermes für iOS

Der native SwiftUI-Client befindet sich in [`TalkToHermes/`](TalkToHermes/). Er setzt iOS 17 oder neuer voraus und verbindet sich ausschließlich über authentifiziertes HTTPS mit einer TalkToHermes Voice Bridge.

## Build

1. `TalkToHermes/TalkToHermes.xcodeproj` in Xcode öffnen.
2. Falls die Gerätesignierung es erfordert, lokal das eigene Development Team auswählen. Persönliche Team- oder Provisioning-Daten nicht committen.
3. Einen iOS-17+-Simulator oder ein entsprechendes Gerät auswählen und das Schema `TalkToHermes` bauen.

Das Repository enthält absichtlich keinen installationsspezifischen Server, keine Signing Identity, keinen Bearer-Token, keine private CA und keinen privaten Schlüssel.

## Bridge konfigurieren

In der App **Einstellungen** öffnen und die vom Bridge-Administrator bereitgestellten Werte eingeben. Beispiel:

| Feld | Beispiel | Hinweise |
|---|---|---|
| Server | `bridge.example.com` | Nur Hostname |
| Port | `8443` | Ganzzahl von `1` bis `65535` |
| Token | vom Administrator bereitgestellter Wert | Wird in der iOS-Keychain gespeichert |

Unter **Server** ausschließlich den Hostnamen eingeben. Nicht `https://`, einen Port, URL-Credentials, Pfad, Query-String oder Fragment eingeben. Die App entfernt Leerraum, wandelt den Hostnamen in Kleinbuchstaben um, entfernt einen abschließenden Punkt und validiert jedes DNS-Label. HTTPS ist fest vorgegeben und kann nicht deaktiviert werden.

Beim Tippen auf **Sichern** ruft die App zunächst authentifiziert `GET /v1/status` auf. Normalisierter Server, Port, Antwortstil, gesprochene Sprache und Keychain-Token werden erst übernommen, wenn die Bridge einen bereiten Status meldet. Die authentifizierte Antwort liefert außerdem die von der App angezeigte Instanzidentität.

Host und Port werden in den App-Einstellungen gespeichert. Der Bearer-Token wird getrennt in der Keychain mit `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` gespeichert; er liegt niemals in Einstellungen oder Quellcode.

## Sprachen

Die App enthält deutsche und englische Lokalisierungen. iOS stellt nach der Installation die normale Sprachauswahl pro App bereit. App-Sprache und gesprochene Sprache sind unabhängig: **Einstellungen → Konversation → Gesprochene Sprache** wählt `Deutsch (de)` oder `Englisch (en)` für nachfolgende Voice-Turns. Bestehende Installationen ohne gültige Einstellung für die gesprochene Sprache verwenden standardmäßig Deutsch; die App leitet die Sprache eines Voice-Turns niemals aus Gerätere­gion oder UI-Lokalisierung ab.

## Sicherheit von Endpunkt und Token

Der gespeicherte Token ist betrieblich an den gespeicherten HTTPS-Origin gebunden:

- Ein leeres Token-Feld verwendet den Keychain-Token nur dann erneut, wenn normalisierter Server und Port unverändert sind.
- Eine Änderung von Server oder Port erfordert die explizite erneute Eingabe des Tokens.
- Fehlgeschlagene Validierung oder ein fehlgeschlagener Status-Check lässt den zuvor gespeicherten Endpunkt und Token unverändert.
- Redirects werden nur verfolgt, wenn Schema, Host und Port identisch bleiben; dadurch kann ein authentifizierter Request nicht auf einen anderen Origin umgeleitet werden.
- Eine Änderung des gespeicherten Endpunkts baut den API-Client neu auf und löscht die aktuelle Konversationsbindung.

Die App verwendet die normale Zertifikatsprüfung von `URLSession`. Es gibt keine Certificate-Pinning-Ausnahme, keinen unsicheren Transport-Fallback und keine Trust-Umgehung.

## Private Zertifizierungsstellen

Eine private Bridge kann eine private CA verwenden. Auf dem verwalteten Gerät ausschließlich das CA-Zertifikat installieren und prüfen und anschließend unter **Einstellungen → Allgemein → Info → Zertifikatsvertrauenseinstellungen** ausdrücklich als vertrauenswürdig aktivieren. Niemals ein privates CA-Zertifikat oder einen privaten Schlüssel in die App packen und niemals die Zertifikatsprüfung deaktivieren.

Das betriebliche Verfahren für CA-Export, Fingerprint-Prüfung und Geräteinstallation ist im [Deployment-Leitfaden](../deployment/README_DE.md#interne-root-ca-unter-ios-installieren-und-vertrauen) beschrieben.

## Verifikation

Vor der Verteilung eines Builds:

1. den vollständigen aktiven Xcode-Testplan ausführen;
2. das Projekt ohne Fehler oder Navigator-Warnungen bauen;
3. einen gültigen Endpunkt speichern und auf einem realen Gerät einen Voice-Turn vollständig durchführen;
4. die App beenden und neu starten und anschließend einen zweiten Voice-Turn durchführen, ohne unveränderte Einstellungen erneut einzugeben;
5. bestätigen, dass eine Änderung von Server oder Port die explizite Token-Eingabe erfordert.
