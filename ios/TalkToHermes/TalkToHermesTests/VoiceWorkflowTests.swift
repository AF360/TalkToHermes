import Foundation
import Testing
@testable import TalkToHermes

struct VoiceWorkflowTests {
    private func completedTurn() throws -> TurnResponse {
        let data = Data(#"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":"Erledigt","input_text":"Öffne das Projekt","tools":["OpenCodeTool","SuperAsteroidsTool"],"error_code":null}"#.utf8)
        return try JSONDecoder().decode(TurnResponse.self, from: data)
    }

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

    @Test func appendsEachCompletedTurnToChatHistoryOnlyOnce() throws {
        var history = ChatHistory()
        let turn = try completedTurn()

        history.append(turn, assistantName: "Klaus")
        history.append(turn, assistantName: "Johanna")

        #expect(history.exchanges == [
            ChatExchange(
                id: "turn-1",
                userText: "Öffne das Projekt",
                assistantText: "Erledigt",
                assistantName: "Klaus",
                tools: ["OpenCodeTool", "SuperAsteroidsTool"]
            )
        ])
    }

    @Test func appendsLegacyCompletedTurnWithoutTranscript() throws {
        let data = Data(#"{"turn_id":"turn-legacy","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":"Die Antwort bleibt sichtbar","error_code":null}"#.utf8)
        let turn = try JSONDecoder().decode(TurnResponse.self, from: data)
        var history = ChatHistory()

        history.append(turn, assistantName: "T’Pol")

        #expect(history.exchanges == [
            ChatExchange(
                id: "turn-legacy",
                userText: nil,
                assistantText: "Die Antwort bleibt sichtbar",
                assistantName: "T’Pol",
                tools: []
            )
        ])
    }

    @Test func formatsToolIdentifiersForChatMetadata() {
        #expect(ChatToolName.display("OpenCodeTool") == "OpenCode Tool")
        #expect(ChatToolName.display("SuperAsteroidsTool") == "SuperAsteroids Tool")
        #expect(ChatToolName.display("terminal") == "Terminal")
    }

    @Test func invalidationRejectsAStaleResponseAudioTicket() {
        var guardState = ResponseAudioGuard()
        let ticket = guardState.makeTicket()

        #expect(guardState.accepts(ticket))
        guardState.invalidate()
        #expect(guardState.accepts(ticket) == false)
    }

    @Test func completedTurnIsTerminalBeforeOptionalAudioPlayback() {
        let state = VoiceTurnControlState.completed

        #expect(state.currentTurnID == nil)
        #expect(state.canCancel == false)
        #expect(state.isBusy == false)
    }

    @Test func conversationResetClearsPreviousResponseAndFallbackNotice() {
        let state = VoiceSessionPresentationState.reset

        #expect(state.responseText.isEmpty)
        #expect(state.degradedAudio == false)
    }

    @Test func bridgeIdentityDetectsInstanceOrAssistantSwitchAtTheSameEndpoint() throws {
        let endpoint = try EndpointConfiguration(host: "bridge.example.com", port: 8_443)
        let klaus = BridgeIdentity(endpoint: endpoint, instanceID: "shared", assistantName: "Klaus")
        let johanna = BridgeIdentity(endpoint: endpoint, instanceID: "johanna", assistantName: "Johanna")
        let renamed = BridgeIdentity(endpoint: endpoint, instanceID: "shared", assistantName: "Johanna")

        #expect(BridgeIdentity.requiresConversationReset(from: nil, to: klaus) == false)
        #expect(BridgeIdentity.requiresConversationReset(from: klaus, to: klaus) == false)
        #expect(BridgeIdentity.requiresConversationReset(from: klaus, to: johanna))
        #expect(BridgeIdentity.requiresConversationReset(from: klaus, to: renamed))
    }

    @Test func usesCompactVoiceStageOnIPhoneWidth() {
        let metrics = VoiceLayout.metrics(for: 390)

        #expect(metrics.mode == .compact)
        #expect(metrics.orbDiameter == 104)
        #expect(metrics.recordButtonDiameter == 72)
    }

    @Test func scrollsWhenAnApprovalRequestAppears() {
        #expect(ChatAutoScroll.shouldRevealApproval(from: false, to: true))
        #expect(ChatAutoScroll.shouldRevealApproval(from: true, to: true) == false)
        #expect(ChatAutoScroll.shouldRevealApproval(from: true, to: false) == false)
    }

    @Test func stacksWideBottomBarForAccessibilityTextSizes() {
        #expect(VoiceLayout.bottomBarMode(isAccessibilitySize: false) == .horizontal)
        #expect(VoiceLayout.bottomBarMode(isAccessibilitySize: true) == .stacked)
    }

    @Test func usesFullWidthChatWithCompactVoiceBarOnIPadWidth() {
        let metrics = VoiceLayout.metrics(for: 1024)

        #expect(metrics.mode == .wide)
        #expect(metrics.orbDiameter == 76)
        #expect(metrics.recordButtonDiameter == 88)
    }
}
