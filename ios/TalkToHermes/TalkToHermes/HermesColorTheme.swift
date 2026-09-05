import SwiftUI

nonisolated enum HermesColorTheme: String, CaseIterable, Identifiable, Sendable {
    case chocolateTruffle = "chocolate-truffle"
    case stormyMorning = "stormy-morning"
    case mossyHollow = "mossy-hollow"

    static let `default`: HermesColorTheme = .chocolateTruffle

    var id: String { rawValue }

    var title: String {
        switch self {
        case .chocolateTruffle:
            String(localized: "Schokoladentrüffel")
        case .stormyMorning:
            String(localized: "Stürmischer Morgen")
        case .mossyHollow:
            String(localized: "Moosige Höhle")
        }
    }

    var palette: HermesPalette {
        switch self {
        case .chocolateTruffle:
            HermesPalette(
                swatches: [0x713600, 0xC05800, 0xFDFBD4, 0x38240D],
                accentIndex: 1,
                lightForegroundIndex: 0,
                darkForegroundIndex: 2,
                messageBackgroundIndex: 0,
                foregroundOnAccentIndex: 2
            )
        case .stormyMorning:
            HermesPalette(
                swatches: [0x6A89A7, 0xBDDDFC, 0x88BDF2, 0x384959],
                accentIndex: 2,
                lightForegroundIndex: 3,
                darkForegroundIndex: 2,
                messageBackgroundIndex: 2,
                foregroundOnAccentIndex: 3
            )
        case .mossyHollow:
            HermesPalette(
                swatches: [0x636B2F, 0xBAC095, 0xD4DE95, 0x3D4127],
                accentIndex: 2,
                lightForegroundIndex: 0,
                darkForegroundIndex: 2,
                messageBackgroundIndex: 2,
                foregroundOnAccentIndex: 3
            )
        }
    }
}

nonisolated enum HermesPaletteAppearance: Equatable, Sendable {
    case light
    case dark
}

nonisolated struct HermesPalette: Equatable, Sendable {
    let swatches: [UInt32]
    private let accentIndex: Int
    private let lightForegroundIndex: Int
    private let darkForegroundIndex: Int
    private let messageBackgroundIndex: Int
    private let foregroundOnAccentIndex: Int

    init(
        swatches: [UInt32],
        accentIndex: Int,
        lightForegroundIndex: Int,
        darkForegroundIndex: Int,
        messageBackgroundIndex: Int,
        foregroundOnAccentIndex: Int
    ) {
        precondition(swatches.count == 4)
        self.swatches = swatches
        self.accentIndex = accentIndex
        self.lightForegroundIndex = lightForegroundIndex
        self.darkForegroundIndex = darkForegroundIndex
        self.messageBackgroundIndex = messageBackgroundIndex
        self.foregroundOnAccentIndex = foregroundOnAccentIndex
    }

    @MainActor var strongAccent: Color { Color(hexRGB: swatches[0]) }
    @MainActor var softAccent: Color { Color(hexRGB: swatches[1]) }
    @MainActor var highlight: Color { Color(hexRGB: swatches[2]) }
    @MainActor var background: Color { Color(hexRGB: swatches[3]) }
    @MainActor var accent: Color { Color(hexRGB: accentRGB) }
    @MainActor var messageBackground: Color { Color(hexRGB: swatches[messageBackgroundIndex]) }
    @MainActor var foregroundOnAccent: Color { Color(hexRGB: swatches[foregroundOnAccentIndex]) }
    @MainActor var waveform: Color { Color(hexRGB: waveformRGB) }

    var accentRGB: UInt32 { swatches[accentIndex] }
    var waveformRGB: UInt32 { swatches[3] }

    func normalForegroundRGB(for appearance: HermesPaletteAppearance) -> UInt32 {
        swatches[appearance == .light ? lightForegroundIndex : darkForegroundIndex]
    }

    func controlAccentRGB(for appearance: HermesPaletteAppearance) -> UInt32 {
        swatches[appearance == .light ? lightForegroundIndex : accentIndex]
    }

    func settingsHeaderIconRGB(for appearance: HermesPaletteAppearance) -> UInt32 {
        normalForegroundRGB(for: appearance)
    }

    @MainActor func normalForeground(for colorScheme: ColorScheme) -> Color {
        Color(hexRGB: normalForegroundRGB(for: colorScheme.paletteAppearance))
    }

    @MainActor func controlAccent(for colorScheme: ColorScheme) -> Color {
        Color(hexRGB: controlAccentRGB(for: colorScheme.paletteAppearance))
    }

    @MainActor func settingsHeaderIcon(for colorScheme: ColorScheme) -> Color {
        Color(hexRGB: settingsHeaderIconRGB(for: colorScheme.paletteAppearance))
    }
}

private extension ColorScheme {
    var paletteAppearance: HermesPaletteAppearance {
        self == .dark ? .dark : .light
    }
}

private struct HermesPaletteEnvironmentKey: EnvironmentKey {
    static let defaultValue = HermesColorTheme.default.palette
}

extension EnvironmentValues {
    var hermesPalette: HermesPalette {
        get { self[HermesPaletteEnvironmentKey.self] }
        set { self[HermesPaletteEnvironmentKey.self] = newValue }
    }
}

extension Color {
    init(hexRGB: UInt32) {
        self.init(
            red: Double((hexRGB >> 16) & 0xFF) / 255,
            green: Double((hexRGB >> 8) & 0xFF) / 255,
            blue: Double(hexRGB & 0xFF) / 255
        )
    }
}
