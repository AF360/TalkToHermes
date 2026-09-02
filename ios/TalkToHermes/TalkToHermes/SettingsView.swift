import SwiftUI

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var host = ""
    @State private var port = "8443"
    @State private var token = ""
    @State private var hasSavedToken = false
    @State private var responseStyle: VoiceResponseStyle = .short
    @State private var speechLanguage: SpeechLanguage = .german
    @State private var isChecking = false
    @State private var errorMessage: String?
    @State private var saveTask: Task<Void, Never>?

    private let store: SettingsStore

    init(store: SettingsStore = SettingsStore()) {
        self.store = store
    }

    var body: some View {
        NavigationStack {
            ZStack {
                HermesBackground()
                Form {
                    Section {
                        HStack(spacing: 15) {
                            Image(systemName: "waveform.badge.shield")
                                .font(.system(size: 27, weight: .semibold))
                                .foregroundStyle(Color.hermesCopper)
                                .frame(width: 52, height: 52)
                                .background(Color.hermesCopper.opacity(0.12), in: RoundedRectangle(cornerRadius: 15))
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Sichere Verbindung")
                                    .font(.headline)
                                Text("Konfigurierbares TLS-Ziel und Token im iOS-Keychain")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    .listRowBackground(Color.clear)

                    Section("Bridge") {
                        LabeledContent("Server") {
                            TextField("bridge.example.com", text: $host)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .keyboardType(.URL)
                                .multilineTextAlignment(.trailing)
                                .font(.subheadline.monospaced())
                                .disabled(isChecking)
                                .accessibilityIdentifier("ServerHostField")
                        }
                        LabeledContent("Port") {
                            TextField("Port", text: $port)
                                .keyboardType(.numberPad)
                                .multilineTextAlignment(.trailing)
                                .frame(maxWidth: 90)
                                .disabled(isChecking)
                        }
                    }

                    Section("Authentifizierung") {
                        SecureField("App-Token", text: $token)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .disabled(isChecking)
                        Label {
                            Text(
                                hasSavedToken
                                    ? String(localized: "Token sicher gespeichert. Leer lassen, um ihn beizubehalten.")
                                    : String(localized: "Noch kein Token gespeichert.")
                            )
                        } icon: {
                            Image(systemName: hasSavedToken ? "checkmark.shield.fill" : "key.fill")
                                .foregroundStyle(hasSavedToken ? Color.green : Color.orange)
                        }
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("TokenStatus")
                    }

                    Section("Gespräch") {
                        Picker("Antwortlänge", selection: $responseStyle) {
                            ForEach(VoiceResponseStyle.allCases) { style in
                                Text(style.title).tag(style)
                            }
                        }
                        .disabled(isChecking)
                        .accessibilityIdentifier("ResponseStylePicker")
                        Picker("Gesprochene Sprache", selection: $speechLanguage) {
                            ForEach(SpeechLanguage.allCases) { language in
                                Text(language.title).tag(language)
                            }
                        }
                        .disabled(isChecking)
                        .accessibilityIdentifier("SpeechLanguagePicker")
                        VStack(alignment: .leading, spacing: 4) {
                            Text(responseStyle.explanation)
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                            Text("Die Auswahl gilt ab dem nächsten Sprachturn.")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                    }

                    Section("Datenschutz") {
                        Label("Audio wird nur für den aktuellen Sprachturn übertragen.", systemImage: "mic.badge.plus")
                        Label("Der Token wird beim Laden nie im Klartext angezeigt.", systemImage: "eye.slash.fill")
                        Label("Die App verwendet keine HTTP-Verbindung und keinen Web-Cache.", systemImage: "lock.fill")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                    Section("App") {
                        LabeledContent("Version", value: versionText)
                        LabeledContent("Host", value: host.isEmpty ? "–" : host)
                    }

                    Section {
                        HStack(spacing: 10) {
                            if isChecking {
                                ProgressView()
                            } else {
                                Image(systemName: "checkmark.circle")
                                    .foregroundStyle(Color.hermesCopper)
                            }
                            Text(
                                isChecking
                                    ? String(localized: "Sichere Verbindung wird geprüft …")
                                    : String(localized: "Beim Sichern wird die Bridge authentifiziert geprüft. Erst danach übernimmt die App die neuen Werte.")
                            )
                        }
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    }
                }
                .scrollContentBackground(.hidden)
            }
            .navigationTitle("Einstellungen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Schließen") { dismiss() }
                        .disabled(isChecking)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Sichern") {
                        guard !isChecking else { return }
                        let hostSnapshot = host
                        let portSnapshot = port
                        let tokenSnapshot = token
                        let responseStyleSnapshot = responseStyle
                        let speechLanguageSnapshot = speechLanguage
                        isChecking = true
                        saveTask = Task {
                            await saveAndVerify(
                                hostText: hostSnapshot,
                                portText: portSnapshot,
                                tokenText: tokenSnapshot,
                                responseStyle: responseStyleSnapshot,
                                speechLanguage: speechLanguageSnapshot
                            )
                        }
                    }
                    .fontWeight(.semibold)
                    .disabled(isChecking)
                }
            }
            .task { load() }
            .interactiveDismissDisabled(isChecking)
            .onDisappear {
                saveTask?.cancel()
                saveTask = nil
            }
            .alert(
                "Einstellungen konnten nicht gespeichert werden",
                isPresented: Binding(
                    get: { errorMessage != nil },
                    set: { if !$0 { errorMessage = nil } }
                )
            ) {
                Button("OK", role: .cancel) { errorMessage = nil }
            } message: {
                Text(errorMessage ?? String(localized: "Unbekannter Fehler"))
            }
        }
        .tint(.hermesCopper)
    }

    private var versionText: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "–"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "–"
        return "\(version) (\(build))"
    }

    private func load() {
        do {
            if let saved = try store.load() {
                host = saved.endpoint.host
                port = String(saved.endpoint.port)
                hasSavedToken = saved.hasToken
                responseStyle = saved.responseStyle
                speechLanguage = saved.speechLanguage
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func saveAndVerify(
        hostText: String,
        portText: String,
        tokenText: String,
        responseStyle: VoiceResponseStyle,
        speechLanguage: SpeechLanguage
    ) async {
        defer {
            isChecking = false
            saveTask = nil
        }
        do {
            let endpoint = try store.validate(hostText: hostText, portText: portText)
            let normalizedToken = tokenText.trimmingCharacters(in: .whitespacesAndNewlines)
            let candidateToken = try store.tokenForValidation(
                endpoint: endpoint, token: normalizedToken
            )
            let client = APIClient(endpoint: endpoint, tokenProvider: { candidateToken })
            let status = try await client.status()
            guard status.status == "ready" else {
                throw APIClientError.invalidResponse
            }
            try Task.checkCancellation()
            _ = try store.save(
                hostText: hostText,
                portText: portText,
                token: normalizedToken,
                responseStyle: responseStyle,
                speechLanguage: speechLanguage
            )
            hasSavedToken = true
            token = ""
            dismiss()
        } catch is CancellationError {
            return
        } catch {
            errorMessage = String(
                format: String(localized: "Die Einstellungen oder der Verbindungstest sind fehlgeschlagen: %@"),
                error.localizedDescription
            )
        }
    }
}

#Preview {
    SettingsView()
}
