import Foundation

nonisolated struct ChatExchange: Identifiable, Equatable, Sendable {
    let id: String
    let userText: String?
    let assistantText: String
    let assistantName: String
    let tools: [String]
}

nonisolated struct ChatHistory: Equatable, Sendable {
    private(set) var exchanges: [ChatExchange] = []

    mutating func append(_ turn: TurnResponse, assistantName: String) {
        let trimmedUserText = turn.inputText?.trimmingCharacters(in: .whitespacesAndNewlines)
        let userText = trimmedUserText.flatMap { $0.isEmpty ? nil : $0 }
        guard turn.phase == .completed,
              let assistantText = turn.responseText?.trimmingCharacters(in: .whitespacesAndNewlines),
              !assistantText.isEmpty,
              !exchanges.contains(where: { $0.id == turn.turnID }) else {
            return
        }
        exchanges.append(
            ChatExchange(
                id: turn.turnID,
                userText: userText,
                assistantText: assistantText,
                assistantName: assistantName,
                tools: turn.tools
            )
        )
    }

    mutating func removeAll() {
        exchanges.removeAll()
    }
}

nonisolated enum ChatToolName {
    static func display(_ identifier: String) -> String {
        let trimmed = identifier.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return identifier }
        if trimmed.hasSuffix("Tool"), trimmed.count > 4 {
            return String(trimmed.dropLast(4)) + " Tool"
        }
        return trimmed.prefix(1).uppercased() + trimmed.dropFirst()
    }
}
