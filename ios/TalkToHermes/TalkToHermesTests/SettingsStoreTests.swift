import Foundation
import Testing
@testable import TalkToHermes

struct SettingsStoreTests {
    private func makeStore() throws -> (String, UserDefaults, KeychainStore, SettingsStore) {
        let suiteName = "systems.acelab.TalkToHermes.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        let keychain = KeychainStore(service: suiteName)
        return (suiteName, defaults, keychain, SettingsStore(defaults: defaults, keychain: keychain))
    }

    @Test func savesValidatedHostPortTokenStyleAndSpeechLanguage() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        let endpoint = try store.save(
            hostText: " Bridge.Example.COM. ",
            portText: "9443",
            token: " private-token ",
            responseStyle: .normal,
            speechLanguage: .english
        )
        #expect(endpoint.host == "bridge.example.com")
        #expect(endpoint.port == 9_443)
        #expect(defaults.string(forKey: SettingsStore.hostKey) == endpoint.host)
        #expect(defaults.integer(forKey: SettingsStore.portKey) == endpoint.port)
        #expect(defaults.string(forKey: SettingsStore.responseStyleKey) == "normal")
        #expect(defaults.string(forKey: SettingsStore.speechLanguageKey) == "en")
        #expect(try keychain.readToken() == "private-token")
    }

    @Test func loadsSavedEndpointAndReportsStoredToken() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        defaults.set("bridge.example.com", forKey: SettingsStore.hostKey)
        defaults.set(8_443, forKey: SettingsStore.portKey)
        try keychain.saveToken("stored-token")
        let loaded = try store.load()
        let saved = try #require(loaded)
        #expect(saved.endpoint.host == "bridge.example.com")
        #expect(saved.endpoint.port == 8_443)
        #expect(saved.hasToken)
        #expect(saved.responseStyle == .short)
        #expect(saved.speechLanguage == .german)
    }

    @Test func migratesMissingOrInvalidSpeechLanguageToGerman() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        defaults.set("bridge.example.com", forKey: SettingsStore.hostKey)
        defaults.set(8_443, forKey: SettingsStore.portKey)

        #expect(try store.load()?.speechLanguage == .german)

        defaults.set("unsupported", forKey: SettingsStore.speechLanguageKey)
        #expect(try store.load()?.speechLanguage == .german)
    }

    @Test(arguments: [SpeechLanguage.german, .english])
    func savesAndLoadsSpeechLanguage(_ speechLanguage: SpeechLanguage) throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }

        _ = try store.save(
            hostText: "bridge.example.com",
            portText: "8443",
            token: "stored-token",
            speechLanguage: speechLanguage
        )

        #expect(try store.load()?.speechLanguage == speechLanguage)
    }

    @Test func preservesTokenForTheSameEndpoint() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        _ = try store.save(
            hostText: "bridge.example.com",
            portText: "8443",
            token: "stored-token",
            speechLanguage: .english
        )
        let endpoint = try store.save(hostText: "BRIDGE.EXAMPLE.COM", portText: "8443", token: "", responseStyle: .detailed)
        let expected = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        #expect(endpoint == expected)
        #expect(try keychain.readToken() == "stored-token")
        #expect(defaults.string(forKey: SettingsStore.responseStyleKey) == "detailed")
        #expect(defaults.string(forKey: SettingsStore.speechLanguageKey) == "en")
    }

    @Test func endpointChangeRequiresAnExplicitNewTokenWithoutLeakingTheOldOne() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        _ = try store.save(hostText: "old.example.com", portText: "8443", token: "old-token")
        let candidate = try EndpointConfiguration(host: "new.example.com", port: 8_443)
        #expect(throws: SettingsStoreError.tokenRequiredForEndpointChange) {
            try store.tokenForValidation(endpoint: candidate, token: "")
        }
        let loaded = try store.load()
        let saved = try #require(loaded)
        #expect(saved.endpoint.host == "old.example.com")
        #expect(try keychain.readToken() == "old-token")
    }

    @Test func savesAChangedEndpointOnlyWithItsExplicitToken() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        _ = try store.save(hostText: "old.example.com", portText: "8443", token: "old-token")
        let endpoint = try store.save(hostText: "new.example.com", portText: "9443", token: "new-token")
        let expected = try EndpointConfiguration(host: "new.example.com", port: 9_443)
        #expect(endpoint == expected)
        #expect(try keychain.readToken() == "new-token")
    }

    @Test func rejectsFirstSaveWithoutToken() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        #expect(throws: SettingsStoreError.tokenRequiredForEndpointChange) {
            try store.save(hostText: "bridge.example.com", portText: "8443", token: "")
        }
    }

    @Test func validatesEndpointWithoutCommittingSettings() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        let endpoint = try store.validate(hostText: "bridge.example.com", portText: "9443")
        #expect(endpoint.host == "bridge.example.com")
        #expect(endpoint.port == 9_443)
        #expect(defaults.object(forKey: SettingsStore.hostKey) == nil)
        #expect(defaults.object(forKey: SettingsStore.portKey) == nil)
    }
}
