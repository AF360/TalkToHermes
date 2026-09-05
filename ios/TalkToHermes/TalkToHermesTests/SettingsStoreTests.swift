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

    @Test func offersTheThreeRequestedColorThemesWithChocolateAsDefault() {
        #expect(HermesColorTheme.allCases == [
            .chocolateTruffle,
            .stormyMorning,
            .mossyHollow,
        ])
        #expect(HermesColorTheme.default == .chocolateTruffle)
        #expect(HermesColorTheme.stormyMorning.palette.swatches == [
            0x6A89A7, 0xBDDDFC, 0x88BDF2, 0x384959,
        ])
        #expect(HermesColorTheme.mossyHollow.palette.swatches == [
            0x636B2F, 0xBAC095, 0xD4DE95, 0x3D4127,
        ])
        #expect(HermesColorTheme.chocolateTruffle.palette.swatches == [
            0x713600, 0xC05800, 0xFDFBD4, 0x38240D,
        ])
    }

    @Test func lightModeNormalForegroundsMeetWCAGContrastOnLightSurfaces() {
        let lightSurfaces: [UInt32] = [0xFFFFFF, 0xF2F2F7]

        for theme in HermesColorTheme.allCases {
            for surface in lightSurfaces {
                #expect(
                    contrastRatio(theme.palette.normalForegroundRGB(for: .light), surface) >= 4.5,
                    "\(theme.rawValue) normal foreground must remain readable on #\(hex(surface))"
                )
            }
        }
    }

    @Test func lightModeControlsAndIconsMeetWCAGContrastOnLightSurfaces() {
        let lightSurfaces: [UInt32] = [0xFFFFFF, 0xF2F2F7]

        for theme in HermesColorTheme.allCases {
            for surface in lightSurfaces {
                #expect(
                    contrastRatio(theme.palette.controlAccentRGB(for: .light), surface) >= 3,
                    "\(theme.rawValue) control accent must remain visible on #\(hex(surface))"
                )
            }
        }
    }

    @Test func darkModeNormalForegroundsMeetWCAGContrastOnPaletteBackgrounds() {
        for theme in HermesColorTheme.allCases {
            #expect(
                contrastRatio(
                    theme.palette.normalForegroundRGB(for: .dark),
                    theme.palette.swatches[3]
                ) >= 4.5,
                "\(theme.rawValue) normal foreground must remain readable in dark mode"
            )
        }
    }

    @Test func darkModeKeepsTheIntendedBrightControlAccents() {
        #expect(HermesColorTheme.chocolateTruffle.palette.controlAccentRGB(for: .dark) == 0xC05800)
        #expect(HermesColorTheme.stormyMorning.palette.controlAccentRGB(for: .dark) == 0x88BDF2)
        #expect(HermesColorTheme.mossyHollow.palette.controlAccentRGB(for: .dark) == 0xD4DE95)
    }

    @Test func voiceOrbWaveformMeetsNonTextContrastAcrossBothGradientEndpoints() {
        for theme in HermesColorTheme.allCases {
            let palette = theme.palette
            let gradientEndpoints = [
                palette.swatches[2],
                palette.controlAccentRGB(for: .dark),
            ]
            for endpoint in gradientEndpoints {
                #expect(
                    contrastRatio(palette.waveformRGB, endpoint) >= 3,
                    "\(theme.rawValue) waveform must remain visible across its voice-orb gradient"
                )
            }
        }
    }

    @Test func settingsHeaderIconMeetsContrastOnItsCompositedBackground() {
        for theme in HermesColorTheme.allCases {
            let palette = theme.palette
            for appearance in [HermesPaletteAppearance.light, .dark] {
                let baseSurface: UInt32 = appearance == .light ? 0xFFFFFF : palette.swatches[3]
                let tintedSurface = compositeRGB(
                    foreground: palette.accentRGB,
                    background: baseSurface,
                    opacity: 0.12
                )
                #expect(
                    contrastRatio(palette.settingsHeaderIconRGB(for: appearance), tintedSurface) >= 3,
                    "\(theme.rawValue) Settings header icon must remain visible in \(appearance) mode"
                )
            }
        }
    }

    @Test func savesValidatedHostPortTokenStyleLanguageAndColorTheme() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        let endpoint = try store.save(
            hostText: " Bridge.Example.COM. ",
            portText: "9443",
            token: " private-token ",
            responseStyle: .normal,
            speechLanguage: .english,
            colorTheme: .stormyMorning
        )
        #expect(endpoint.host == "bridge.example.com")
        #expect(endpoint.port == 9_443)
        #expect(defaults.string(forKey: SettingsStore.hostKey) == endpoint.host)
        #expect(defaults.integer(forKey: SettingsStore.portKey) == endpoint.port)
        #expect(defaults.string(forKey: SettingsStore.responseStyleKey) == "normal")
        #expect(defaults.string(forKey: SettingsStore.speechLanguageKey) == "en")
        #expect(defaults.string(forKey: SettingsStore.colorThemeKey) == "stormy-morning")
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
        #expect(saved.colorTheme == .chocolateTruffle)
    }

    @Test func migratesMissingOrInvalidColorThemeToChocolateTruffle() throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }
        defaults.set("bridge.example.com", forKey: SettingsStore.hostKey)
        defaults.set(8_443, forKey: SettingsStore.portKey)

        #expect(try store.load()?.colorTheme == .chocolateTruffle)

        defaults.set("unsupported", forKey: SettingsStore.colorThemeKey)
        #expect(try store.load()?.colorTheme == .chocolateTruffle)
    }

    @Test(arguments: HermesColorTheme.allCases)
    func savesAndLoadsColorTheme(_ colorTheme: HermesColorTheme) throws {
        let (suiteName, defaults, keychain, store) = try makeStore()
        defer { defaults.removePersistentDomain(forName: suiteName); try? keychain.deleteToken() }

        _ = try store.save(
            hostText: "bridge.example.com",
            portText: "8443",
            token: "stored-token",
            colorTheme: colorTheme
        )

        #expect(try store.load()?.colorTheme == colorTheme)
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

private func compositeRGB(foreground: UInt32, background: UInt32, opacity: Double) -> UInt32 {
    func component(_ rgb: UInt32, shift: UInt32) -> Double {
        Double((rgb >> shift) & 0xFF)
    }
    func blended(_ shift: UInt32) -> UInt32 {
        UInt32((component(foreground, shift: shift) * opacity +
                component(background, shift: shift) * (1 - opacity)).rounded())
    }
    return (blended(16) << 16) | (blended(8) << 8) | blended(0)
}

private func contrastRatio(_ first: UInt32, _ second: UInt32) -> Double {
    let lighter = max(relativeLuminance(first), relativeLuminance(second))
    let darker = min(relativeLuminance(first), relativeLuminance(second))
    return (lighter + 0.05) / (darker + 0.05)
}

private func relativeLuminance(_ rgb: UInt32) -> Double {
    func linearized(_ component: UInt32) -> Double {
        let value = Double(component) / 255
        return value <= 0.04045 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
    }

    let red = linearized((rgb >> 16) & 0xFF)
    let green = linearized((rgb >> 8) & 0xFF)
    let blue = linearized(rgb & 0xFF)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

private func hex(_ rgb: UInt32) -> String {
    String(format: "%06X", rgb)
}
