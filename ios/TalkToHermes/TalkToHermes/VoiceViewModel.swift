import AVFoundation
import Combine
import Foundation

@MainActor
final class VoiceViewModel: ObservableObject {
    @Published private(set) var statusText = String(localized: "Konfiguration fehlt")
    @Published private(set) var assistantName = "Hermes"
    @Published private(set) var responseText = ""
    @Published private(set) var isReady = false
    @Published private(set) var isBusy = false
    @Published private(set) var isStartingRecording = false
    @Published private(set) var isRefreshingConfiguration = false
    @Published private(set) var canCancel = false
    @Published private(set) var approvalRequired = false
    @Published private(set) var degradedAudio = false
    @Published private(set) var isPlaying = false
    @Published private(set) var hasPlayableResponse = false
    @Published private(set) var chatHistory = ChatHistory()
    @Published var errorMessage: String?

    let recorder = VoiceRecorder()

    private let settingsStore: SettingsStore
    private var client: APIClient?
    private var bridgeIdentity: BridgeIdentity?
    private var conversationID: String?
    private var currentTurnID: String?
    private var monitorTask: Task<Void, Never>?
    private var recordingStartTask: Task<Void, Never>?
    private var recordingLimitTask: Task<Void, Never>?
    private var player: AVAudioPlayer?
    private var playbackTask: Task<Void, Never>?
    private var responseAudioGuard = ResponseAudioGuard()
    private var configurationRefreshGeneration = 0

    init(settingsStore: SettingsStore = SettingsStore()) {
        self.settingsStore = settingsStore
#if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--ui-test-tool-activity") {
            chatHistory = ChatHistory(exchanges: [
                ChatExchange(
                    id: "ui-scroll-probe-1",
                    userText: "Fasse die wichtigsten Punkte für den Testverlauf zusammen.",
                    assistantText: String(
                        repeating: "Dieser längere Testeintrag macht die Unterhaltung zuverlässig scrollbar. ",
                        count: 5
                    ),
                    assistantName: "Hermes"
                ),
                ChatExchange(
                    id: "ui-scroll-probe-2",
                    userText: "Ergänze einen zweiten Abschnitt.",
                    assistantText: String(
                        repeating: "Auch dieser Inhalt gehört ausschließlich zum lokalen UI-Test. ",
                        count: 5
                    ),
                    assistantName: "Hermes"
                ),
                ChatExchange(
                    id: "ui-tool-turn",
                    userText: "Öffne das Projekt und prüfe das Wetter.",
                    assistantText: "Beide Tool-Aufrufe sind abgeschlossen.",
                    assistantName: "Klaus",
                    toolInvocations: [
                        ChatToolInvocation(
                            id: "tool-6",
                            name: "read_file",
                            summary: "Datei gelesen",
                            status: "invoked",
                            startedAt: "2026-09-04T17:00:00Z",
                            approvalRequired: false,
                            risk: nil
                        ),
                        ChatToolInvocation(
                            id: "tool-7",
                            name: "mcp__home_assistant__ha_get_state",
                            summary: "Status in Home Assistant abgerufen",
                            status: "invoked",
                            startedAt: "2026-09-04T17:00:01Z",
                            approvalRequired: true,
                            risk: "medium"
                        ),
                    ]
                )
            ])
        }
#endif
    }

    var canSpeak: Bool {
        isReady && !isBusy && !isStartingRecording && !isRefreshingConfiguration &&
        !canCancel && !approvalRequired
    }

    func refreshConfiguration() async {
#if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--ui-test-tool-activity") { return }
#endif
        guard !isBusy, !isStartingRecording, !canCancel, !recorder.isRecording else { return }
        configurationRefreshGeneration &+= 1
        let generation = configurationRefreshGeneration
        isRefreshingConfiguration = true
        defer {
            if generation == configurationRefreshGeneration {
                isRefreshingConfiguration = false
            }
        }
        do {
            guard let saved = try settingsStore.load(), saved.hasToken else {
                guard generation == configurationRefreshGeneration else { return }
                client = nil
                assistantName = "Hermes"
                isReady = false
                statusText = String(localized: "Konfiguration fehlt")
                return
            }
            let keychain = settingsStore.keychain
            let newClient = APIClient(
                endpoint: saved.endpoint,
                tokenProvider: { try keychain.readToken() }
            )
            statusText = String(localized: "Verbindung wird geprüft …")
            let status = try await newClient.status()
            guard generation == configurationRefreshGeneration else { return }
            guard status.status == "ready" else {
                throw APIClientError.invalidResponse
            }
            let newIdentity = BridgeIdentity(
                endpoint: newClient.endpoint,
                instanceID: status.instanceID,
                assistantName: status.assistantName
            )
            if BridgeIdentity.requiresConversationReset(from: bridgeIdentity, to: newIdentity) {
                invalidatePendingResponseAudio()
                conversationID = nil
                currentTurnID = nil
                apply(.reset)
                chatHistory.removeAll()
                stopPlayback(clearAudio: true)
            }
            bridgeIdentity = newIdentity
            assistantName = status.assistantName
            client = newClient
            isReady = true
            statusText = String(localized: "Bereit")
        } catch {
            guard generation == configurationRefreshGeneration else { return }
            client = nil
            assistantName = "Hermes"
            isReady = false
            statusText = String(localized: "Nicht verbunden")
            errorMessage = String(
                format: String(localized: "Die sichere Verbindung zur Bridge konnte nicht geprüft werden: %@"),
                error.localizedDescription
            )
        }
    }

    func toggleRecording() {
        if recorder.isRecording {
            do {
                recordingLimitTask?.cancel()
                recordingLimitTask = nil
                let url = try recorder.stop()
                beginSubmission(recordingURL: url)
            } catch {
                fail(error)
            }
        } else {
            guard !isStartingRecording else { return }
            invalidatePendingResponseAudio()
            if isPlaying {
                stopResponsePlayback()
            }
            isStartingRecording = true
            recordingStartTask = Task { [weak self] in
                guard let self else { return }
                defer {
                    self.isStartingRecording = false
                    self.recordingStartTask = nil
                }
                do {
                    try await self.recorder.start()
                    try Task.checkCancellation()
                    self.stopPlayback(clearAudio: true)
                    self.statusText = String(localized: "Aufnahme läuft – zum Beenden erneut tippen")
                    self.recordingLimitTask?.cancel()
                    self.recordingLimitTask = Task { [weak self] in
                        try? await Task.sleep(for: .seconds(VoiceRecorder.maximumDuration))
                        guard let self, self.recorder.isRecording else { return }
                        do {
                            let url = try self.recorder.stop()
                            self.beginSubmission(recordingURL: url)
                        } catch {
                            self.fail(error)
                        }
                    }
                } catch {
                    self.recorder.discard()
                    if !(error is CancellationError) {
                        self.fail(error)
                    }
                }
            }
        }
    }

    func approveOnce() {
        resolveApproval(decision: "once")
    }

    func denyApproval() {
        resolveApproval(decision: "deny")
    }

    func cancelTurn() {
        responseAudioGuard.invalidate()
        recordingStartTask?.cancel()
        recordingStartTask = nil
        isStartingRecording = false
        recordingLimitTask?.cancel()
        recordingLimitTask = nil
        recorder.discard()
        monitorTask?.cancel()
        guard let client, let turnID = currentTurnID else {
            currentTurnID = nil
            approvalRequired = false
            canCancel = false
            isBusy = false
            statusText = String(localized: "Abgebrochen")
            return
        }
        approvalRequired = false
        canCancel = true
        isBusy = true
        statusText = String(localized: "Abbruch wird bestätigt …")
        monitorTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await client.cancel(turnID)
                try await self.waitForCancellation(turnID: turnID, client: client)
            } catch is CancellationError {
                return
            } catch {
                self.canCancel = true
                self.isBusy = true
                self.statusText = String(localized: "Abbruch noch nicht bestätigt")
                self.errorMessage = String(
                    format: String(localized: "Der Turn konnte nicht sauber abgebrochen werden. Du kannst den Abbruch erneut versuchen: %@"),
                    error.localizedDescription
                )
            }
        }
    }

    func newConversation() {
        guard !isBusy, !isStartingRecording, !canCancel, !recorder.isRecording else { return }
        invalidatePendingResponseAudio()
        let previous = conversationID
        conversationID = nil
        currentTurnID = nil
        apply(.reset)
        chatHistory.removeAll()
        stopPlayback(clearAudio: true)
        statusText = isReady ? String(localized: "Bereit") : statusText
        guard let client, let previous else { return }
        Task {
            do {
                try await client.deleteConversation(previous)
            } catch {
                errorMessage = String(
                    format: String(localized: "Die vorherige Unterhaltung konnte nicht gelöscht werden: %@"),
                    error.localizedDescription
                )
            }
        }
    }

    func prepareForSettings() {
        invalidatePendingResponseAudio()
        stopResponsePlayback()
    }

    func togglePlayback() {
        guard hasPlayableResponse, let player else { return }
        if player.isPlaying {
            player.pause()
            playbackTask?.cancel()
            playbackTask = nil
            isPlaying = false
        } else {
            if player.currentTime >= player.duration - 0.05 {
                player.currentTime = 0
            }
            guard player.play() else {
                isPlaying = false
                return
            }
            isPlaying = true
            monitorPlayback()
        }
    }

    func stopResponsePlayback() {
        playbackTask?.cancel()
        playbackTask = nil
        player?.stop()
        player?.currentTime = 0
        isPlaying = false
    }

    private func beginSubmission(recordingURL: URL) {
        responseAudioGuard.invalidate()
        monitorTask?.cancel()
        stopPlayback(clearAudio: true)
        isBusy = true
        canCancel = false
        approvalRequired = false
        degradedAudio = false
        statusText = String(localized: "Audio wird übertragen …")
        monitorTask = Task { [weak self] in
            await self?.submit(recordingURL: recordingURL)
        }
    }

    private func submit(recordingURL: URL) async {
        defer { try? FileManager.default.removeItem(at: recordingURL) }
        do {
            guard let client else { throw APIClientError.invalidResponse }
            let values = try recordingURL.resourceValues(forKeys: [.fileSizeKey])
            guard let fileSize = values.fileSize, fileSize <= APIClient.maxAudioBytes else {
                throw APIClientError.audioTooLarge
            }
            let audio = try Data(contentsOf: recordingURL, options: .mappedIfSafe)
            let savedSettings = try settingsStore.load()
            let responseStyle = savedSettings?.responseStyle ?? .short
            let speechLanguage = savedSettings?.speechLanguage ?? .german
            let conversation: String
            if let conversationID {
                conversation = conversationID
            } else {
                let created = try await client.createConversation()
                conversationID = created.conversationID
                conversation = created.conversationID
            }
            let accepted = try await client.createVoiceTurn(
                conversationID: conversation,
                clientTurnID: UUID().uuidString,
                audio: audio,
                language: speechLanguage.rawValue,
                responseStyle: responseStyle
            )
            currentTurnID = accepted.turnID
            canCancel = true
            statusText = String(format: String(localized: "%@ verarbeitet die Anfrage …"), assistantName)
            try await monitor(turnID: accepted.turnID, client: client)
        } catch is CancellationError {
            return
        } catch {
            fail(error)
        }
    }

    private func monitor(turnID: String, client: APIClient) async throws {
        for _ in 0..<400 {
            try Task.checkCancellation()
            let turn = try await client.turn(turnID)
            switch turn.phase {
            case .processing:
                statusText = status(for: turn.state)
            case .awaitingApproval:
                approvalRequired = true
                isBusy = false
                canCancel = true
                statusText = String(format: String(localized: "%@ benötigt deine Freigabe"), assistantName)
                return
            case .completed:
                responseText = turn.responseText ?? ""
                chatHistory.append(turn, assistantName: assistantName)
                degradedAudio = turn.degradedLocalAudio
                statusText = turn.degradedLocalAudio
                    ? String(localized: "Antwort bereit (Fallback-Stimme)")
                    : String(localized: "Antwort bereit")
                let audioTicket = responseAudioGuard.makeTicket()
                apply(.completed)
                do {
                    let audio = try await client.audio(turnID)
                    try Task.checkCancellation()
                    guard responseAudioGuard.accepts(audioTicket) else { return }
                    try play(audio)
                } catch is CancellationError {
                    return
                } catch {
                    guard responseAudioGuard.accepts(audioTicket) else { return }
                    stopPlayback(clearAudio: true)
                    statusText = String(localized: "Antwort bereit – Audio nicht verfügbar")
                    errorMessage = String(
                        format: String(localized: "Die Antwort ist verfügbar, konnte aber nicht abgespielt werden: %@"),
                        error.localizedDescription
                    )
                }
                return
            case .failed:
                currentTurnID = nil
                canCancel = false
                isBusy = false
                throw APIClientError.invalidResponse
            case .cancelled:
                currentTurnID = nil
                canCancel = false
                isBusy = false
                statusText = String(localized: "Abgebrochen")
                return
            }
            try await Task.sleep(for: .milliseconds(750))
        }
        throw URLError(.timedOut)
    }

    private func resolveApproval(decision: String) {
        guard let client, let turnID = currentTurnID else { return }
        approvalRequired = false
        isBusy = true
        canCancel = true
        statusText = decision == "once"
            ? String(localized: "Freigabe wird übermittelt …")
            : String(localized: "Ablehnung wird übermittelt …")
        monitorTask?.cancel()
        monitorTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await client.approve(turnID, decision: decision)
                try await self.monitor(turnID: turnID, client: client)
            } catch is CancellationError {
                return
            } catch {
                self.fail(error)
            }
        }
    }

    private func status(for state: String) -> String {
        switch state {
        case "transcribing": String(localized: "Sprache wird erkannt …")
        case "thinking": String(format: String(localized: "%@ denkt nach …"), assistantName)
        case "synthesizing": String(localized: "Antwort wird gesprochen …")
        default: String(format: String(localized: "%@ verarbeitet die Anfrage …"), assistantName)
        }
    }

    private func fail(_ error: Error) {
        approvalRequired = false
        if currentTurnID != nil {
            isBusy = true
            canCancel = true
            statusText = String(localized: "Verbindung unterbrochen – Turn bitte abbrechen")
        } else {
            isBusy = false
            canCancel = false
            statusText = String(localized: "Fehler")
        }
        errorMessage = error.localizedDescription
    }

    private func apply(_ state: VoiceTurnControlState) {
        currentTurnID = state.currentTurnID
        canCancel = state.canCancel
        isBusy = state.isBusy
    }

    private func apply(_ state: VoiceSessionPresentationState) {
        responseText = state.responseText
        degradedAudio = state.degradedAudio
    }

    private func invalidatePendingResponseAudio() {
        responseAudioGuard.invalidate()
        monitorTask?.cancel()
        monitorTask = nil
    }

    private func waitForCancellation(turnID: String, client: APIClient) async throws {
        for _ in 0..<60 {
            try Task.checkCancellation()
            let turn = try await client.turn(turnID)
            if turn.phase.isTerminal {
                currentTurnID = nil
                canCancel = false
                isBusy = false
                statusText = String(localized: "Abgebrochen")
                return
            }
            try await Task.sleep(for: .milliseconds(500))
        }
        throw URLError(.timedOut)
    }

    private func play(_ audio: Data) throws {
        stopPlayback(clearAudio: false)
        player = try AVAudioPlayer(data: audio)
        guard player?.prepareToPlay() == true, player?.play() == true else {
            player = nil
            hasPlayableResponse = false
            throw APIClientError.invalidResponse
        }
        hasPlayableResponse = true
        isPlaying = true
        monitorPlayback()
    }

    private func monitorPlayback() {
        playbackTask?.cancel()
        playbackTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(200))
                guard !Task.isCancelled, let self else { return }
                guard self.player?.isPlaying == true else {
                    self.isPlaying = false
                    self.playbackTask = nil
                    return
                }
            }
        }
    }

    private func stopPlayback(clearAudio: Bool) {
        playbackTask?.cancel()
        playbackTask = nil
        player?.stop()
        player = nil
        isPlaying = false
        if clearAudio {
            hasPlayableResponse = false
        }
    }
}
