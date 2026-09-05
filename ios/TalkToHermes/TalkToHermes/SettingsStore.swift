import Foundation

nonisolated struct StoredSettings: Equatable, Sendable {
    let endpoint: EndpointConfiguration
    let hasToken: Bool
    let responseStyle: VoiceResponseStyle
    let speechLanguage: SpeechLanguage
    let colorTheme: HermesColorTheme
}

nonisolated enum SettingsStoreError: Error, Equatable, LocalizedError {
    case tokenRequiredForEndpointChange

    var errorDescription: String? {
        switch self {
        case .tokenRequiredForEndpointChange:
            String(localized: "Für einen neuen oder geänderten Server muss der zugehörige Token erneut eingegeben werden.")
        }
    }
}

nonisolated struct SettingsStore {
    static let hostKey = "bridge-host"
    static let portKey = "bridge-port"
    static let responseStyleKey = "voice-response-style"
    static let speechLanguageKey = "speech-language"
    static let colorThemeKey = "color-theme"

    let defaults: UserDefaults
    let keychain: KeychainStore

    init(
        defaults: UserDefaults = .standard,
        keychain: KeychainStore = KeychainStore()
    ) {
        self.defaults = defaults
        self.keychain = keychain
    }

    @discardableResult
    func save(
        hostText: String,
        portText: String,
        token: String,
        responseStyle: VoiceResponseStyle = .short,
        speechLanguage: SpeechLanguage? = nil,
        colorTheme: HermesColorTheme? = nil
    ) throws -> EndpointConfiguration {
        let endpoint = try validate(hostText: hostText, portText: portText)
        let normalizedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        _ = try tokenForValidation(endpoint: endpoint, token: normalizedToken)
        if !normalizedToken.isEmpty {
            try keychain.saveToken(normalizedToken)
        }
        defaults.set(endpoint.host, forKey: Self.hostKey)
        defaults.set(endpoint.port, forKey: Self.portKey)
        defaults.set(responseStyle.rawValue, forKey: Self.responseStyleKey)
        let effectiveSpeechLanguage = speechLanguage
            ?? defaults.string(forKey: Self.speechLanguageKey)
                .flatMap(SpeechLanguage.init(rawValue:))
            ?? .german
        defaults.set(effectiveSpeechLanguage.rawValue, forKey: Self.speechLanguageKey)
        let effectiveColorTheme = colorTheme
            ?? defaults.string(forKey: Self.colorThemeKey)
                .flatMap(HermesColorTheme.init(rawValue:))
            ?? .default
        defaults.set(effectiveColorTheme.rawValue, forKey: Self.colorThemeKey)
        return endpoint
    }

    func validate(hostText: String, portText: String) throws -> EndpointConfiguration {
        let normalizedPort = portText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let port = Int(normalizedPort) else {
            throw EndpointConfigurationError.invalidPort
        }
        return try EndpointConfiguration(host: hostText, port: port)
    }

    func tokenForValidation(endpoint: EndpointConfiguration, token: String) throws -> String {
        let normalizedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        if !normalizedToken.isEmpty {
            return normalizedToken
        }
        guard let saved = try load(), saved.endpoint == endpoint else {
            throw SettingsStoreError.tokenRequiredForEndpointChange
        }
        return try keychain.readToken()
    }

    func load() throws -> StoredSettings? {
        guard let host = defaults.string(forKey: Self.hostKey),
              defaults.object(forKey: Self.portKey) != nil else {
            return nil
        }
        let endpoint = try EndpointConfiguration(
            host: host,
            port: defaults.integer(forKey: Self.portKey)
        )
        let responseStyle = defaults.string(forKey: Self.responseStyleKey)
            .flatMap(VoiceResponseStyle.init(rawValue:)) ?? .short
        let speechLanguage = defaults.string(forKey: Self.speechLanguageKey)
            .flatMap(SpeechLanguage.init(rawValue:)) ?? .german
        let colorTheme = defaults.string(forKey: Self.colorThemeKey)
            .flatMap(HermesColorTheme.init(rawValue:)) ?? .default
        do {
            _ = try keychain.readToken()
            return StoredSettings(
                endpoint: endpoint,
                hasToken: true,
                responseStyle: responseStyle,
                speechLanguage: speechLanguage,
                colorTheme: colorTheme
            )
        } catch KeychainStoreError.itemNotFound {
            return StoredSettings(
                endpoint: endpoint,
                hasToken: false,
                responseStyle: responseStyle,
                speechLanguage: speechLanguage,
                colorTheme: colorTheme
            )
        }
    }
}
