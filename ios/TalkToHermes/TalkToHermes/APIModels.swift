import Foundation

nonisolated struct BridgeIdentity: Equatable, Sendable {
    let endpoint: EndpointConfiguration
    let instanceID: String
    let assistantName: String

    static func requiresConversationReset(
        from previous: BridgeIdentity?,
        to current: BridgeIdentity
    ) -> Bool {
        guard let previous else { return false }
        return previous != current
    }
}

nonisolated struct VoiceTurnControlState: Equatable, Sendable {
    let currentTurnID: String?
    let canCancel: Bool
    let isBusy: Bool

    static let completed = VoiceTurnControlState(
        currentTurnID: nil,
        canCancel: false,
        isBusy: false
    )
}

nonisolated struct VoiceSessionPresentationState: Equatable, Sendable {
    let responseText: String
    let degradedAudio: Bool

    static let reset = VoiceSessionPresentationState(
        responseText: "",
        degradedAudio: false
    )
}

nonisolated struct ResponseAudioTicket: Equatable, Sendable {
    fileprivate let generation: UInt
}

nonisolated struct ResponseAudioGuard: Equatable, Sendable {
    private var generation: UInt = 0

    func makeTicket() -> ResponseAudioTicket {
        ResponseAudioTicket(generation: generation)
    }

    func accepts(_ ticket: ResponseAudioTicket) -> Bool {
        ticket.generation == generation
    }

    mutating func invalidate() {
        generation &+= 1
    }
}

nonisolated struct StatusResponse: Decodable, Equatable, Sendable {
    let status: String
    let instanceID: String
    let assistantName: String

    enum CodingKeys: String, CodingKey {
        case status
        case instanceID = "instance_id"
        case assistantName = "assistant_name"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        status = try values.decode(String.self, forKey: .status)
        instanceID = try values.decode(String.self, forKey: .instanceID)
        if values.contains(.assistantName) {
            let explicitName = try values.decode(String.self, forKey: .assistantName)
            guard (1...64).contains(explicitName.count) else {
                throw DecodingError.dataCorruptedError(
                    forKey: .assistantName,
                    in: values,
                    debugDescription: "assistant_name must contain 1 through 64 characters"
                )
            }
            assistantName = explicitName
        } else if let legacyName = ["klaus": "Klaus", "johanna": "Johanna"][instanceID] {
            assistantName = legacyName
        } else {
            throw DecodingError.keyNotFound(
                CodingKeys.assistantName,
                DecodingError.Context(
                    codingPath: values.codingPath,
                    debugDescription: "assistant_name is required for unknown instances"
                )
            )
        }
    }
}

nonisolated struct ConversationResponse: Decodable, Equatable, Sendable {
    let conversationID: String
    let createdAt: String
    let updatedAt: String

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

nonisolated struct TurnAcceptedResponse: Decodable, Equatable, Sendable {
    let turnID: String
    let state: String
    let eventsURL: String

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case state
        case eventsURL = "events_url"
    }
}

nonisolated struct ToolInvocation: Decodable, Equatable, Identifiable, Sendable {
    let id: String
    let name: String
    let summary: String?
    let status: String?
    let startedAt: String?
    let approvalRequired: Bool
    let risk: String?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case summary
        case status
        case startedAt = "started_at"
        case approvalRequired = "approval_required"
        case risk
    }

    init(
        id: String,
        name: String,
        summary: String?,
        status: String?,
        startedAt: String?,
        approvalRequired: Bool,
        risk: String?
    ) {
        self.id = id
        self.name = name
        self.summary = summary
        self.status = status
        self.startedAt = startedAt
        self.approvalRequired = approvalRequired
        self.risk = risk
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        name = try values.decode(String.self, forKey: .name)
        summary = try? values.decodeIfPresent(String.self, forKey: .summary)
        status = try? values.decodeIfPresent(String.self, forKey: .status)
        startedAt = try? values.decodeIfPresent(String.self, forKey: .startedAt)
        approvalRequired = (try? values.decodeIfPresent(Bool.self, forKey: .approvalRequired)) ?? false
        risk = try? values.decodeIfPresent(String.self, forKey: .risk)
    }
}

private struct LossyToolInvocation: Decodable {
    let value: ToolInvocation?

    init(from decoder: Decoder) throws {
        value = try? ToolInvocation(from: decoder)
    }
}

nonisolated struct TurnResponse: Decodable, Equatable, Sendable {
    let turnID: String
    let conversationID: String
    let clientTurnID: String
    let state: String
    let createdAt: String
    let updatedAt: String
    let responseText: String?
    let inputText: String?
    let tools: [String]
    let toolInvocations: [ToolInvocation]
    let errorCode: String?
    let degradedLocalAudio: Bool

    enum CodingKeys: String, CodingKey {
        case turnID = "turn_id"
        case conversationID = "conversation_id"
        case clientTurnID = "client_turn_id"
        case state
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case responseText = "response_text"
        case inputText = "input_text"
        case tools
        case toolInvocations = "tool_invocations"
        case errorCode = "error_code"
        case degradedLocalAudio = "degraded_local_audio"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        turnID = try values.decode(String.self, forKey: .turnID)
        conversationID = try values.decode(String.self, forKey: .conversationID)
        clientTurnID = try values.decode(String.self, forKey: .clientTurnID)
        state = try values.decode(String.self, forKey: .state)
        createdAt = try values.decode(String.self, forKey: .createdAt)
        updatedAt = try values.decode(String.self, forKey: .updatedAt)
        responseText = try values.decodeIfPresent(String.self, forKey: .responseText)
        inputText = try values.decodeIfPresent(String.self, forKey: .inputText)
        tools = try values.decodeIfPresent([String].self, forKey: .tools) ?? []
        let decodedToolInvocations = try? values.decode(
            [LossyToolInvocation].self, forKey: .toolInvocations
        )
        toolInvocations = (decodedToolInvocations ?? []).compactMap(\.value)
        errorCode = try values.decodeIfPresent(String.self, forKey: .errorCode)
        degradedLocalAudio = try values.decodeIfPresent(Bool.self, forKey: .degradedLocalAudio) ?? false
    }
}

nonisolated enum VoiceTurnPhase: Equatable, Sendable {
    case processing
    case awaitingApproval
    case completed
    case failed
    case cancelled

    var isTerminal: Bool {
        switch self {
        case .completed, .failed, .cancelled: true
        case .processing, .awaitingApproval: false
        }
    }
}

nonisolated extension TurnResponse {
    var phase: VoiceTurnPhase {
        switch state {
        case "awaiting_approval": .awaitingApproval
        case "completed": .completed
        case "failed": .failed
        case "cancelled": .cancelled
        default: .processing
        }
    }
}

nonisolated struct ApprovalRequest: Encodable, Equatable, Sendable {
    let decision: String
}
