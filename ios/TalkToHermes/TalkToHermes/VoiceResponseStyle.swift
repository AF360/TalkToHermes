import Foundation

nonisolated enum VoiceResponseStyle: String, CaseIterable, Codable, Identifiable, Sendable {
    case short
    case normal
    case detailed

    var id: String { rawValue }

    var title: String {
        switch self {
        case .short: String(localized: "Kurz")
        case .normal: String(localized: "Normal")
        case .detailed: String(localized: "Ausführlich")
        }
    }

    var explanation: String {
        switch self {
        case .short: String(localized: "Direkt, meist ein bis drei Sätze")
        case .normal: String(localized: "Kompakt mit den wichtigsten Details")
        case .detailed: String(localized: "Ausführliche gesprochene Erklärung")
        }
    }
}
