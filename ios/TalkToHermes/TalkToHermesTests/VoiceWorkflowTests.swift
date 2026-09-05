import Foundation
import Testing
@testable import TalkToHermes

struct VoiceWorkflowTests {
    private func completedTurn() throws -> TurnResponse {
        let data = Data(#"{"turn_id":"turn-1","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-08-29T12:00:00Z","updated_at":"2026-08-29T12:01:00Z","response_text":"Erledigt","input_text":"Öffne das Projekt","tools":["read_file","web_search"],"error_code":null}"#.utf8)
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
                tools: ["read_file", "web_search"]
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
        #expect(ChatToolName.display("read_file") == "Read File")
        #expect(ChatToolName.display("web_search") == "Web Search")
        #expect(
            ChatToolName.display("mcp__home_assistant__ha_get_state") ==
                "Home Assistant · HA Get State"
        )
        #expect(ChatToolName.display("terminal") == "Terminal")
    }

    @Test func preservesDuplicateDetailedToolCallsAndBuildsLegacyMarkers() throws {
        let detailedData = Data(#"{"turn_id":"turn-detailed","conversation_id":"conversation-1","client_turn_id":"client-1","state":"completed","created_at":"2026-09-04T17:00:00Z","updated_at":"2026-09-04T17:00:02Z","response_text":"Erledigt","tool_invocations":[{"id":"tool-6","name":"read_file","summary":"Datei gelesen","status":"invoked","started_at":"2026-09-04T17:00:00Z","approval_required":false},{"id":"tool-7","name":"read_file","summary":"Datei erneut gelesen","status":"invoked","started_at":"2026-09-04T17:00:01Z","approval_required":true,"risk":"medium"}]}"#.utf8)
        let legacyData = Data(#"{"turn_id":"turn-legacy-tools","conversation_id":"conversation-1","client_turn_id":"client-2","state":"completed","created_at":"2026-09-04T17:01:00Z","updated_at":"2026-09-04T17:01:02Z","response_text":"Alt","tools":["mcp__home_assistant__ha_get_state"]}"#.utf8)
        var history = ChatHistory()

        history.append(
            try JSONDecoder().decode(TurnResponse.self, from: detailedData),
            assistantName: "Klaus"
        )
        history.append(
            try JSONDecoder().decode(TurnResponse.self, from: legacyData),
            assistantName: "Klaus"
        )

        #expect(history.exchanges[0].toolInvocations.map(\.name) == ["read_file", "read_file"])
        #expect(history.exchanges[0].toolInvocations.map(\.summary) == ["Datei gelesen", "Datei erneut gelesen"])
        #expect(history.exchanges[1].toolInvocations == [
            ChatToolInvocation.legacy(id: "legacy-0", name: "mcp__home_assistant__ha_get_state")
        ])
    }

    @Test func toolMarkersUseUniqueTurnScopedIdentifiersAndAccessibleTouchTargets() {
        let invocation = ChatToolInvocation.legacy(id: "legacy-0", name: "mcp__home_assistant__ha_get_state")

        #expect(
            invocation.accessibilityIdentifier(exchangeID: "turn-a") ==
                "ToolInvocation-turn-a-legacy-0"
        )
        #expect(ToolActivityLayout.markerTouchDiameter == 44)
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

    @Test func pinsBrandHeaderAboveCompactConversationScroll() {
        #expect(VoiceLayout.pinsBrandHeader(for: .compact))
    }

    @Test func requiresExplicitConfirmationBeforeStartingNewConversation() {
        #expect(NewConversationDecision.cancel.startsNewConversation == false)
        #expect(NewConversationDecision.confirm.startsNewConversation)
    }

    @Test func usesCompactToolbarOrbOnIPhoneWidth() {
        let metrics = VoiceLayout.metrics(for: 390)

        #expect(metrics.mode == .compact)
        #expect(metrics.orbDiameter == 104)
        #expect(metrics.toolbarOrbDiameter == 38)
        #expect(metrics.showsInlineVoiceOrb == false)
        #expect(metrics.recordButtonDiameter == 64)
        #expect(metrics.brandHeaderTopPadding == 0)
        #expect(metrics.bottomContentPadding == 0)
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
        #expect(metrics.recordButtonDiameter == 76)
        #expect(metrics.bottomContentPadding == 0)
    }
}
