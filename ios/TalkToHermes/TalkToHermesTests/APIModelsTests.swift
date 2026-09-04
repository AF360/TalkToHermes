import Foundation
import Testing
@testable import TalkToHermes

struct APIModelsTests {
    @Test func decodesConversationResponseFromBridgeJSON() throws {
        let data = Data(#"{"conversation_id":"conversation-1","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z"}"#.utf8)

        let response = try JSONDecoder().decode(ConversationResponse.self, from: data)

        #expect(response.conversationID == "conversation-1")
        #expect(response.createdAt == "2026-08-29T12:00:00Z")
        #expect(response.updatedAt == "2026-08-29T12:01:00Z")
    }

    @Test func decodesAcceptedTurnAndEventsURL() throws {
        let data = Data(#"{"turn_id":"turn-1","state":"accepted","events_url":"/v1/turns/turn-1/events"}"#.utf8)

        let response = try JSONDecoder().decode(TurnAcceptedResponse.self, from: data)

        #expect(response.turnID == "turn-1")
        #expect(response.state == "accepted")
        #expect(response.eventsURL == "/v1/turns/turn-1/events")
    }

    @Test func decodesCompletedTurnWithOptionalResponseText() throws {
        let data = Data(#"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":"Hallo Welt","error_code":null}"#.utf8)

        let response = try JSONDecoder().decode(TurnResponse.self, from: data)

        #expect(response.turnID == "turn-1")
        #expect(response.conversationID == "conversation-1")
        #expect(response.clientTurnID == "client-1")
        #expect(response.state == "completed")
        #expect(response.responseText == "Hallo Welt")
        #expect(response.errorCode == nil)
        #expect(response.degradedLocalAudio == false)
    }

    @Test func decodesDegradedFallbackFlag() throws {
        let data = Data(#"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":null,"error_code":null,"degraded_local_audio":true}"#.utf8)

        let response = try JSONDecoder().decode(TurnResponse.self, from: data)

        #expect(response.degradedLocalAudio)
        #expect(response.inputText == nil)
        #expect(response.tools.isEmpty)
    }

    @Test func decodesTranscriptAndToolNamesForChatHistory() throws {
        let data = Data(#"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":"Erledigt","input_text":"Öffne das Projekt","tools":["OpenCodeTool","SuperAsteroidsTool"],"error_code":null}"#.utf8)

        let response = try JSONDecoder().decode(TurnResponse.self, from: data)

        #expect(response.inputText == "Öffne das Projekt")
        #expect(response.tools == ["OpenCodeTool", "SuperAsteroidsTool"])
    }

    @Test func decodesKnownLegacyStatusWithoutAssistantName() throws {
        let data = Data(#"{"status":"ready","instance_id":"johanna"}"#.utf8)

        let response = try JSONDecoder().decode(StatusResponse.self, from: data)

        #expect(response.assistantName == "Johanna")
    }

    @Test func decodesConfiguredAssistantName() throws {
        let data = Data(#"{"status":"ready","instance_id":"t-pol","assistant_name":"T’Pol"}"#.utf8)

        let response = try JSONDecoder().decode(StatusResponse.self, from: data)

        #expect(response.assistantName == "T’Pol")
    }

    @Test func rejectsExplicitNullAssistantNameForLegacyInstance() {
        let data = Data(#"{"status":"ready","instance_id":"klaus","assistant_name":null}"#.utf8)

        #expect(throws: DecodingError.self) {
            try JSONDecoder().decode(StatusResponse.self, from: data)
        }
    }

    @Test func rejectsEmptyConfiguredAssistantName() {
        let data = Data(#"{"status":"ready","instance_id":"empty","assistant_name":""}"#.utf8)

        #expect(throws: DecodingError.self) {
            try JSONDecoder().decode(StatusResponse.self, from: data)
        }
    }

    @Test func rejectsOverlongConfiguredAssistantName() {
        let longName = String(repeating: "A", count: 65)
        let json = #"{"status":"ready","instance_id":"long","assistant_name":"NAME"}"#
            .replacingOccurrences(of: "NAME", with: longName)

        #expect(throws: DecodingError.self) {
            try JSONDecoder().decode(StatusResponse.self, from: Data(json.utf8))
        }
    }

    @Test func decodesAuthenticatedStatus() throws {
        let data = Data(#"{"status":"ready","instance_id":"johanna","assistant_name":"Johanna"}"#.utf8)

        let response = try JSONDecoder().decode(StatusResponse.self, from: data)

        #expect(response.status == "ready")
        #expect(response.instanceID == "johanna")
        #expect(response.assistantName == "Johanna")
    }
}
