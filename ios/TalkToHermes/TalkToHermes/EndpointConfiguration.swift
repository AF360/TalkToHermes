import Foundation

nonisolated enum EndpointConfigurationError: Error, Equatable, LocalizedError {
    case invalidHost
    case invalidPort

    var errorDescription: String? {
        switch self {
        case .invalidHost:
            String(localized: "Der Servername ist ungültig.")
        case .invalidPort:
            String(localized: "Der Port muss zwischen 1 und 65535 liegen.")
        }
    }
}

nonisolated struct EndpointConfiguration: Equatable, Sendable {
    let host: String
    let port: Int

    init(host: String, port: Int) throws {
        let normalizedHost = Self.normalize(host)
        guard Self.isValid(normalizedHost) else {
            throw EndpointConfigurationError.invalidHost
        }
        guard (1...65_535).contains(port) else {
            throw EndpointConfigurationError.invalidPort
        }
        self.host = normalizedHost
        self.port = port
    }

    var baseURL: URL {
        get throws {
            var components = URLComponents()
            components.scheme = "https"
            components.host = host
            components.port = port
            guard let url = components.url,
                  url.scheme == "https",
                  url.host?.lowercased() == host else {
                throw URLError(.badURL)
            }
            return url
        }
    }

    private static func normalize(_ value: String) -> String {
        var normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        while normalized.hasSuffix(".") {
            normalized.removeLast()
        }
        return normalized
    }

    private static func isValid(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 253 else { return false }
        let labels = value.split(separator: ".", omittingEmptySubsequences: false)
        guard !labels.isEmpty else { return false }
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789-")
        return labels.allSatisfy { label in
            guard !label.isEmpty, label.utf8.count <= 63,
                  label.first != "-", label.last != "-" else { return false }
            return label.unicodeScalars.allSatisfy { allowed.contains($0) }
        }
    }
}
