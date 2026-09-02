import Foundation
import Testing
@testable import TalkToHermes

struct VoiceWorkflowTests {
    @Test func mapsApprovalStateToActionablePhase() throws {
        let data = Data(#"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"awaiting_approval","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":null,"error_code":null,"degraded_local_audio":false}"#.utf8)

        let response = try JSONDecoder().decode(TurnResponse.self, from: data)

        #expect(response.phase == .awaitingApproval)
        #expect(!response.phase.isTerminal)
    }

    @Test func mapsCompletedAndFailedStatesToTerminalPhases() throws {
        func response(state: String) throws -> TurnResponse {
            let json = #"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"STATE","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":null,"error_code":null,"degraded_local_audio":false}"#
                .replacingOccurrences(of: "STATE", with: state)
            return try JSONDecoder().decode(TurnResponse.self, from: Data(json.utf8))
        }

        #expect(try response(state: "completed").phase == .completed)
        #expect(try response(state: "failed").phase == .failed)
        #expect(try response(state: "cancelled").phase == .cancelled)
        #expect(try response(state: "completed").phase.isTerminal)
    }
}
