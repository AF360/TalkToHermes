import Foundation

nonisolated struct StatusResponse: Decodable, Equatable, Sendable {
    let status: String
    let instanceID: String

    enum CodingKeys: String, CodingKey {
        case status
        case instanceID = "instance_id"
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

nonisolated struct TurnResponse: Decodable, Equatable, Sendable {
    let turnID: String
    let conversationID: String
    let clientTurnID: String
    let state: String
    let createdAt: String
    let updatedAt: String
    let responseText: String?
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
