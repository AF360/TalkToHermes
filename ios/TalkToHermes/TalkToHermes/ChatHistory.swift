import CoreGraphics
import Foundation

nonisolated struct ChatToolInvocation: Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    let summary: String?
    let status: String?
    let startedAt: String?
    let approvalRequired: Bool?
    let risk: String?

    static func detailed(_ invocation: ToolInvocation) -> ChatToolInvocation {
        ChatToolInvocation(
            id: invocation.id,
            name: invocation.name,
            summary: invocation.summary,
            status: invocation.status,
            startedAt: invocation.startedAt,
            approvalRequired: invocation.approvalRequired,
            risk: invocation.risk
        )
    }

    static func legacy(id: String, name: String) -> ChatToolInvocation {
        ChatToolInvocation(
            id: id,
            name: name,
            summary: nil,
            status: nil,
            startedAt: nil,
            approvalRequired: nil,
            risk: nil
        )
    }

    func accessibilityIdentifier(exchangeID: String) -> String {
        "ToolInvocation-\(exchangeID)-\(id)"
    }
}

nonisolated struct ChatExchange: Identifiable, Equatable, Sendable {
    let id: String
    let userText: String?
    let assistantText: String
    let assistantName: String
    let toolInvocations: [ChatToolInvocation]

    init(
        id: String,
        userText: String?,
        assistantText: String,
        assistantName: String,
        tools: [String] = [],
        toolInvocations: [ChatToolInvocation] = []
    ) {
        self.id = id
        self.userText = userText
        self.assistantText = assistantText
        self.assistantName = assistantName
        self.toolInvocations = toolInvocations.isEmpty
            ? tools.enumerated().map {
                ChatToolInvocation.legacy(id: "legacy-\($0.offset)", name: $0.element)
            }
            : toolInvocations
    }

    var tools: [String] {
        toolInvocations.map(\.name)
    }
}

nonisolated struct ChatHistory: Equatable, Sendable {
    private(set) var exchanges: [ChatExchange] = []

    init(exchanges: [ChatExchange] = []) {
        self.exchanges = exchanges
    }

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
                tools: turn.tools,
                toolInvocations: turn.toolInvocations.map(ChatToolInvocation.detailed)
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

        let mcpParts = trimmed.components(separatedBy: "__")
        if mcpParts.count > 1 {
            let visibleParts = mcpParts.first?.lowercased() == "mcp"
                ? Array(mcpParts.dropFirst())
                : mcpParts
            return visibleParts.map(titleize).joined(separator: " · ")
        }

        let namespaceParts = trimmed.components(separatedBy: ".")
        if namespaceParts.count > 1 {
            return namespaceParts.map(titleize).joined(separator: " · ")
        }

        if trimmed.hasSuffix("Tool"), trimmed.count > 4 {
            return String(trimmed.dropLast(4)) + " Tool"
        }
        return titleize(trimmed)
    }

    private static func titleize(_ value: String) -> String {
        value
            .split(whereSeparator: { $0 == "_" || $0 == "-" })
            .map { word in
                let text = String(word)
                if ["api", "ha", "mcp", "stt", "tts"].contains(text.lowercased()) {
                    return text.uppercased()
                }
                return text.prefix(1).uppercased() + text.dropFirst()
            }
            .joined(separator: " ")
    }
}

nonisolated enum ToolActivityLayout {
    static let markerTouchDiameter: CGFloat = 44
}
