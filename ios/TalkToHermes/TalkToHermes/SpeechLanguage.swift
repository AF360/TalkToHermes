import Foundation

nonisolated enum SpeechLanguage: String, CaseIterable, Codable, Identifiable, Sendable {
    case german = "de"
    case english = "en"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .german: String(localized: "Deutsch")
        case .english: String(localized: "Englisch")
        }
    }
}
